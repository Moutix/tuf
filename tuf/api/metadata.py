# Copyright New York University and the TUF contributors
# SPDX-License-Identifier: MIT OR Apache-2.0

"""TUF role metadata model.

This module provides container classes for TUF role metadata, including methods
to read and write from and to file, perform TUF-compliant metadata updates, and
create and verify signatures.

The metadata model supports any custom serialization format, defaulting to JSON
as wireline format and Canonical JSON for reproducible signature creation and
verification.
Custom serializers must implement the abstract serialization interface defined
in 'tuf.api.serialization', and may use the [to|from]_dict convenience methods
available in the class model.

"""
import tempfile
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional

from securesystemslib.keys import verify_signature
from securesystemslib.signer import Signature, Signer
from securesystemslib.storage import FilesystemBackend, StorageBackendInterface
from securesystemslib.util import persist_temp_file

from tuf import exceptions, formats
from tuf.api.serialization import (
    MetadataDeserializer,
    MetadataSerializer,
    SignedSerializer,
)


class Metadata:
    """A container for signed TUF metadata.

    Provides methods to convert to and from dictionary, read and write to and
    from file and to create and verify metadata signatures.

    Attributes:
        signed: A subclass of Signed, which has the actual metadata payload,
            i.e. one of Targets, Snapshot, Timestamp or Root.

        signatures: A list of Securesystemslib Signature objects, each signing
            the canonical serialized representation of 'signed'.

    """

    def __init__(self, signed: "Signed", signatures: List[Signature]) -> None:
        self.signed = signed
        self.signatures = signatures

    @classmethod
    def from_dict(cls, metadata: Dict[str, Any]) -> "Metadata":
        """Creates Metadata object from its dict representation.

        Arguments:
            metadata: TUF metadata in dict representation.

        Raises:
            KeyError: The metadata dict format is invalid.
            ValueError: The metadata has an unrecognized signed._type field.

        Side Effect:
            Destroys the metadata dict passed by reference.

        Returns:
            A TUF Metadata object.

        """
        # Dispatch to contained metadata class on metadata _type field.
        _type = metadata["signed"]["_type"]

        if _type == "targets":
            inner_cls = Targets
        elif _type == "snapshot":
            inner_cls = Snapshot
        elif _type == "timestamp":
            inner_cls = Timestamp
        elif _type == "root":
            inner_cls = Root
        else:
            raise ValueError(f'unrecognized metadata type "{_type}"')

        signatures = []
        for signature in metadata.pop("signatures"):
            signature_obj = Signature.from_dict(signature)
            signatures.append(signature_obj)

        return cls(
            signed=inner_cls.from_dict(metadata.pop("signed")),
            signatures=signatures,
        )

    @classmethod
    def from_file(
        cls,
        filename: str,
        deserializer: Optional[MetadataDeserializer] = None,
        storage_backend: Optional[StorageBackendInterface] = None,
    ) -> "Metadata":
        """Loads TUF metadata from file storage.

        Arguments:
            filename: The path to read the file from.
            deserializer: A MetadataDeserializer subclass instance that
                implements the desired wireline format deserialization. Per
                default a JSONDeserializer is used.
            storage_backend: An object that implements
                securesystemslib.storage.StorageBackendInterface. Per default
                a (local) FilesystemBackend is used.

        Raises:
            securesystemslib.exceptions.StorageError: The file cannot be read.
            tuf.api.serialization.DeserializationError:
                The file cannot be deserialized.

        Returns:
            A TUF Metadata object.

        """
        if storage_backend is None:
            storage_backend = FilesystemBackend()

        with storage_backend.get(filename) as file_obj:
            return cls.from_bytes(file_obj.read(), deserializer)

    @staticmethod
    def from_bytes(
        data: bytes,
        deserializer: Optional[MetadataDeserializer] = None,
    ) -> "Metadata":
        """Loads TUF metadata from raw data.

        Arguments:
            data: metadata content as bytes.
            deserializer: Optional; A MetadataDeserializer instance that
                implements deserialization. Default is JSONDeserializer.

        Raises:
            tuf.api.serialization.DeserializationError:
                The file cannot be deserialized.

        Returns:
            A TUF Metadata object.
        """

        if deserializer is None:
            # Use local scope import to avoid circular import errors
            # pylint: disable=import-outside-toplevel
            from tuf.api.serialization.json import JSONDeserializer

            deserializer = JSONDeserializer()

        return deserializer.deserialize(data)

    def to_dict(self) -> Dict[str, Any]:
        """Returns the dict representation of self."""

        signatures = []
        for sig in self.signatures:
            signatures.append(sig.to_dict())

        return {"signatures": signatures, "signed": self.signed.to_dict()}

    def to_file(
        self,
        filename: str,
        serializer: Optional[MetadataSerializer] = None,
        storage_backend: Optional[StorageBackendInterface] = None,
    ) -> None:
        """Writes TUF metadata to file storage.

        Arguments:
            filename: The path to write the file to.
            serializer: A MetadataSerializer subclass instance that implements
                the desired wireline format serialization. Per default a
                JSONSerializer is used.
            storage_backend: An object that implements
                securesystemslib.storage.StorageBackendInterface. Per default
                a (local) FilesystemBackend is used.

        Raises:
            tuf.api.serialization.SerializationError:
                The metadata object cannot be serialized.
            securesystemslib.exceptions.StorageError:
                The file cannot be written.

        """
        if serializer is None:
            # Use local scope import to avoid circular import errors
            # pylint: disable=import-outside-toplevel
            from tuf.api.serialization.json import JSONSerializer

            serializer = JSONSerializer(compact=True)

        with tempfile.TemporaryFile() as temp_file:
            temp_file.write(serializer.serialize(self))
            persist_temp_file(temp_file, filename, storage_backend)

    # Signatures.
    def sign(
        self,
        signer: Signer,
        append: bool = False,
        signed_serializer: Optional[SignedSerializer] = None,
    ) -> Dict[str, Any]:
        """Creates signature over 'signed' and assigns it to 'signatures'.

        Arguments:
            signer: An object implementing the securesystemslib.signer.Signer
                interface.
            append: A boolean indicating if the signature should be appended to
                the list of signatures or replace any existing signatures. The
                default behavior is to replace signatures.
            signed_serializer: A SignedSerializer subclass instance that
                implements the desired canonicalization format. Per default a
                CanonicalJSONSerializer is used.

        Raises:
            tuf.api.serialization.SerializationError:
                'signed' cannot be serialized.
            securesystemslib.exceptions.CryptoError, \
                    securesystemslib.exceptions.UnsupportedAlgorithmError:
                Signing errors.

        Returns:
            A securesystemslib-style signature object.

        """
        if signed_serializer is None:
            # Use local scope import to avoid circular import errors
            # pylint: disable=import-outside-toplevel
            from tuf.api.serialization.json import CanonicalJSONSerializer

            signed_serializer = CanonicalJSONSerializer()

        signature = signer.sign(signed_serializer.serialize(self.signed))

        if append:
            self.signatures.append(signature)
        else:
            self.signatures = [signature]

        return signature

    def verify(
        self,
        key: Mapping[str, Any],
        signed_serializer: Optional[SignedSerializer] = None,
    ) -> bool:
        """Verifies 'signatures' over 'signed' that match the passed key by id.

        Arguments:
            key: A securesystemslib-style public key object.
            signed_serializer: A SignedSerializer subclass instance that
                implements the desired canonicalization format. Per default a
                CanonicalJSONSerializer is used.

        Raises:
            # TODO: Revise exception taxonomy
            tuf.exceptions.Error: None or multiple signatures found for key.
            securesystemslib.exceptions.FormatError: Key argument is malformed.
            tuf.api.serialization.SerializationError:
                'signed' cannot be serialized.
            securesystemslib.exceptions.CryptoError, \
                    securesystemslib.exceptions.UnsupportedAlgorithmError:
                Signing errors.

        Returns:
            A boolean indicating if the signature is valid for the passed key.

        """
        signatures_for_keyid = list(
            filter(lambda sig: sig.keyid == key["keyid"], self.signatures)
        )

        if not signatures_for_keyid:
            raise exceptions.Error(f"no signature for key {key['keyid']}.")

        if len(signatures_for_keyid) > 1:
            raise exceptions.Error(
                f"{len(signatures_for_keyid)} signatures for key "
                f"{key['keyid']}, not sure which one to verify."
            )

        if signed_serializer is None:
            # Use local scope import to avoid circular import errors
            # pylint: disable=import-outside-toplevel
            from tuf.api.serialization.json import CanonicalJSONSerializer

            signed_serializer = CanonicalJSONSerializer()

        return verify_signature(
            key,
            signatures_for_keyid[0].to_dict(),
            signed_serializer.serialize(self.signed),
        )


class Signed:
    """A base class for the signed part of TUF metadata.

    Objects with base class Signed are usually included in a Metadata object
    on the signed attribute. This class provides attributes and methods that
    are common for all TUF metadata types (roles).

    Attributes:
        _type: The metadata type string. Also available without underscore.
        version: The metadata version number.
        spec_version: The TUF specification version number (semver) the
            metadata format adheres to.
        expires: The metadata expiration datetime object.
        unrecognized_fields: Dictionary of all unrecognized fields.
    """

    # Signed implementations are expected to override this
    _signed_type = None

    # _type and type are identical: 1st replicates file format, 2nd passes lint
    @property
    def _type(self):
        return self._signed_type

    @property
    def type(self):
        return self._signed_type

    # NOTE: Signed is a stupid name, because this might not be signed yet, but
    # we keep it to match spec terminology (I often refer to this as "payload",
    # or "inner metadata")
    def __init__(
        self,
        version: int,
        spec_version: str,
        expires: datetime,
        unrecognized_fields: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.spec_version = spec_version
        self.expires = expires

        # TODO: Should we separate data validation from constructor?
        if version <= 0:
            raise ValueError(f"version must be > 0, got {version}")
        self.version = version
        self.unrecognized_fields: Mapping[str, Any] = unrecognized_fields or {}

    @classmethod
    def _common_fields_from_dict(cls, signed_dict: Dict[str, Any]) -> List[Any]:
        """Returns common fields of 'Signed' instances from the passed dict
        representation, and returns an ordered list to be passed as leading
        positional arguments to a subclass constructor.

        See '{Root, Timestamp, Snapshot, Targets}.from_dict' methods for usage.

        """
        _type = signed_dict.pop("_type")
        if _type != cls._signed_type:
            raise ValueError(f"Expected type {cls._signed_type}, got {_type}")

        version = signed_dict.pop("version")
        spec_version = signed_dict.pop("spec_version")
        expires_str = signed_dict.pop("expires")
        # Convert 'expires' TUF metadata string to a datetime object, which is
        # what the constructor expects and what we store. The inverse operation
        # is implemented in '_common_fields_to_dict'.
        expires = formats.expiry_string_to_datetime(expires_str)
        return [version, spec_version, expires]

    def _common_fields_to_dict(self) -> Dict[str, Any]:
        """Returns dict representation of common fields of 'Signed' instances.

        See '{Root, Timestamp, Snapshot, Targets}.to_dict' methods for usage.

        """
        return {
            "_type": self._type,
            "version": self.version,
            "spec_version": self.spec_version,
            "expires": self.expires.isoformat() + "Z",
            **self.unrecognized_fields,
        }

    def is_expired(self, reference_time: datetime = None) -> bool:
        """Checks metadata expiration against a reference time.

        Args:
            reference_time: Optional; The time to check expiration date against.
                A naive datetime in UTC expected.
                If not provided, checks against the current UTC date and time.

        Returns:
            True if expiration time is less than the reference time.
        """
        if reference_time is None:
            reference_time = datetime.utcnow()

        return reference_time >= self.expires

    # Modification.
    def bump_expiration(self, delta: timedelta = timedelta(days=1)) -> None:
        """Increments the expires attribute by the passed timedelta."""
        self.expires += delta

    def bump_version(self) -> None:
        """Increments the metadata version number by 1."""
        self.version += 1


class Key:
    """A container class representing the public portion of a Key.

    Attributes:
        keytype: A string denoting a public key signature system,
            such as "rsa", "ed25519", and "ecdsa-sha2-nistp256".
        scheme: A string denoting a corresponding signature scheme. For example:
            "rsassa-pss-sha256", "ed25519", and "ecdsa-sha2-nistp256".
        keyval: A dictionary containing the public portion of the key.
        unrecognized_fields: Dictionary of all unrecognized fields.

    """

    def __init__(
        self,
        keytype: str,
        scheme: str,
        keyval: Dict[str, str],
        unrecognized_fields: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if not keyval.get("public"):
            raise ValueError("keyval doesn't follow the specification format!")
        self.keytype = keytype
        self.scheme = scheme
        self.keyval = keyval
        self.unrecognized_fields: Mapping[str, Any] = unrecognized_fields or {}

    @classmethod
    def from_dict(cls, key_dict: Dict[str, Any]) -> "Key":
        """Creates Key object from its dict representation."""
        keytype = key_dict.pop("keytype")
        scheme = key_dict.pop("scheme")
        keyval = key_dict.pop("keyval")
        # All fields left in the key_dict are unrecognized.
        return cls(keytype, scheme, keyval, key_dict)

    def to_dict(self) -> Dict[str, Any]:
        """Returns the dictionary representation of self."""
        return {
            "keytype": self.keytype,
            "scheme": self.scheme,
            "keyval": self.keyval,
            **self.unrecognized_fields,
        }


class Role:
    """A container class containing the set of keyids and threshold associated
    with a particular role.

    Attributes:
        keyids: A set of strings each of which represents a given key.
        threshold: An integer representing the required number of keys for that
            particular role.
        unrecognized_fields: Dictionary of all unrecognized fields.

    """

    def __init__(
        self,
        keyids: List[str],
        threshold: int,
        unrecognized_fields: Optional[Mapping[str, Any]] = None,
    ) -> None:
        keyids_set = set(keyids)
        if len(keyids_set) != len(keyids):
            raise ValueError(
                f"keyids should be a list of unique strings,"
                f" instead got {keyids}"
            )
        self.keyids = keyids_set
        self.threshold = threshold
        self.unrecognized_fields: Mapping[str, Any] = unrecognized_fields or {}

    @classmethod
    def from_dict(cls, role_dict: Dict[str, Any]) -> "Role":
        """Creates Role object from its dict representation."""
        keyids = role_dict.pop("keyids")
        threshold = role_dict.pop("threshold")
        # All fields left in the role_dict are unrecognized.
        return cls(keyids, threshold, role_dict)

    def to_dict(self) -> Dict[str, Any]:
        """Returns the dictionary representation of self."""
        return {
            "keyids": list(self.keyids),
            "threshold": self.threshold,
            **self.unrecognized_fields,
        }


class Root(Signed):
    """A container for the signed part of root metadata.

    Attributes:
        consistent_snapshot: A boolean indicating whether the repository
            supports consistent snapshots.
        keys: A dictionary that contains a public key store used to verify
            top level roles metadata signatures::

                {
                    '<KEYID>': <Key instance>,
                    ...
                },

        roles: A dictionary that contains a list of signing keyids and
            a signature threshold for each top level role::

                {
                    '<ROLE>': <Role istance>,
                    ...
                }

    """

    _signed_type = "root"

    # TODO: determine an appropriate value for max-args and fix places where
    # we violate that. This __init__ function takes 7 arguments, whereas the
    # default max-args value for pylint is 5
    # pylint: disable=too-many-arguments
    def __init__(
        self,
        version: int,
        spec_version: str,
        expires: datetime,
        consistent_snapshot: bool,
        keys: Dict[str, Key],
        roles: Dict[str, Role],
        unrecognized_fields: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(version, spec_version, expires, unrecognized_fields)
        self.consistent_snapshot = consistent_snapshot
        self.keys = keys
        self.roles = roles

    @classmethod
    def from_dict(cls, root_dict: Dict[str, Any]) -> "Root":
        """Creates Root object from its dict representation."""
        common_args = cls._common_fields_from_dict(root_dict)
        consistent_snapshot = root_dict.pop("consistent_snapshot")
        keys = root_dict.pop("keys")
        roles = root_dict.pop("roles")

        for keyid, key_dict in keys.items():
            keys[keyid] = Key.from_dict(key_dict)
        for role_name, role_dict in roles.items():
            roles[role_name] = Role.from_dict(role_dict)

        # All fields left in the root_dict are unrecognized.
        return cls(*common_args, consistent_snapshot, keys, roles, root_dict)

    def to_dict(self) -> Dict[str, Any]:
        """Returns the dict representation of self."""
        root_dict = self._common_fields_to_dict()
        keys = {keyid: key.to_dict() for (keyid, key) in self.keys.items()}
        roles = {}
        for role_name, role in self.roles.items():
            roles[role_name] = role.to_dict()

        root_dict.update(
            {
                "consistent_snapshot": self.consistent_snapshot,
                "keys": keys,
                "roles": roles,
            }
        )
        return root_dict

    # Update key for a role.
    def add_key(
        self, role: str, keyid: str, key_metadata: Dict[str, Any]
    ) -> None:
        """Adds new key for 'role' and updates the key store."""
        self.roles[role].keyids.add(keyid)
        self.keys[keyid] = key_metadata

    def remove_key(self, role: str, keyid: str) -> None:
        """Removes key from 'role' and updates the key store.

        Raises:
            KeyError: If 'role' does not include the key
        """
        self.roles[role].keyids.remove(keyid)
        for keyinfo in self.roles.values():
            if keyid in keyinfo.keyids:
                return

        del self.keys[keyid]


class Timestamp(Signed):
    """A container for the signed part of timestamp metadata.

    Attributes:
        meta: A dictionary that contains information about snapshot metadata::

            {
                'snapshot.json': {
                    'version': <SNAPSHOT METADATA VERSION NUMBER>,
                    'length': <SNAPSHOT METADATA FILE SIZE>, // optional
                    'hashes': {
                        '<HASH ALGO 1>': '<SNAPSHOT METADATA FILE HASH 1>',
                        '<HASH ALGO 2>': '<SNAPSHOT METADATA FILE HASH 2>',
                        ...
                    } // optional
                }
            }

    """

    _signed_type = "timestamp"

    def __init__(
        self,
        version: int,
        spec_version: str,
        expires: datetime,
        meta: Dict[str, Any],
        unrecognized_fields: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(version, spec_version, expires, unrecognized_fields)
        # TODO: Add class for meta
        self.meta = meta

    @classmethod
    def from_dict(cls, timestamp_dict: Dict[str, Any]) -> "Timestamp":
        """Creates Timestamp object from its dict representation."""
        common_args = cls._common_fields_from_dict(timestamp_dict)
        meta = timestamp_dict.pop("meta")
        # All fields left in the timestamp_dict are unrecognized.
        return cls(*common_args, meta, timestamp_dict)

    def to_dict(self) -> Dict[str, Any]:
        """Returns the dict representation of self."""
        timestamp_dict = self._common_fields_to_dict()
        timestamp_dict.update({"meta": self.meta})
        return timestamp_dict

    # Modification.
    def update(
        self,
        version: int,
        length: Optional[int] = None,
        hashes: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Assigns passed info about snapshot metadata to meta dict."""
        self.meta["snapshot.json"] = {"version": version}

        if length is not None:
            self.meta["snapshot.json"]["length"] = length

        if hashes is not None:
            self.meta["snapshot.json"]["hashes"] = hashes


class Snapshot(Signed):
    """A container for the signed part of snapshot metadata.

    Attributes:
        meta: A dictionary that contains information about targets metadata::

            {
                'targets.json': {
                    'version': <TARGETS METADATA VERSION NUMBER>,
                    'length': <TARGETS METADATA FILE SIZE>, // optional
                    'hashes': {
                        '<HASH ALGO 1>': '<TARGETS METADATA FILE HASH 1>',
                        '<HASH ALGO 2>': '<TARGETS METADATA FILE HASH 2>',
                        ...
                    } // optional
                },
                '<DELEGATED TARGETS ROLE 1>.json': {
                    ...
                },
                '<DELEGATED TARGETS ROLE 2>.json': {
                    ...
                },
                ...
            }

    """

    _signed_type = "snapshot"

    def __init__(
        self,
        version: int,
        spec_version: str,
        expires: datetime,
        meta: Dict[str, Any],
        unrecognized_fields: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(version, spec_version, expires, unrecognized_fields)
        # TODO: Add class for meta
        self.meta = meta

    @classmethod
    def from_dict(cls, snapshot_dict: Dict[str, Any]) -> "Snapshot":
        """Creates Snapshot object from its dict representation."""
        common_args = cls._common_fields_from_dict(snapshot_dict)
        meta = snapshot_dict.pop("meta")
        # All fields left in the snapshot_dict are unrecognized.
        return cls(*common_args, meta, snapshot_dict)

    def to_dict(self) -> Dict[str, Any]:
        """Returns the dict representation of self."""
        snapshot_dict = self._common_fields_to_dict()
        snapshot_dict.update({"meta": self.meta})
        return snapshot_dict

    # Modification.
    def update(
        self,
        rolename: str,
        version: int,
        length: Optional[int] = None,
        hashes: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Assigns passed (delegated) targets role info to meta dict."""
        metadata_fn = f"{rolename}.json"

        self.meta[metadata_fn] = {"version": version}
        if length is not None:
            self.meta[metadata_fn]["length"] = length

        if hashes is not None:
            self.meta[metadata_fn]["hashes"] = hashes


class DelegatedRole(Role):
    """A container with information about particular delegated role.

    Attributes:
        name: A string giving the name of the delegated role.
        keyids: A set of strings each of which represents a given key.
        threshold: An integer representing the required number of keys for that
            particular role.
        terminating: A boolean indicating whether subsequent delegations
            should be considered.
        paths: An optional list of strings, where each string describes
            a path that the role is trusted to provide.
        path_hash_prefixes: An optional list of HEX_DIGESTs used to succinctly
            describe a set of target paths. Only one of the attributes "paths"
            and "path_hash_prefixes" is allowed to be set.
        unrecognized_fields: Dictionary of all unrecognized fields.

    """

    def __init__(
        self,
        name: str,
        keyids: List[str],
        threshold: int,
        terminating: bool,
        paths: Optional[List[str]] = None,
        path_hash_prefixes: Optional[List[str]] = None,
        unrecognized_fields: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(keyids, threshold, unrecognized_fields)
        self.name = name
        self.terminating = terminating
        if paths and path_hash_prefixes:
            raise ValueError(
                "Only one of the attributes 'paths' and"
                "'path_hash_prefixes' can be set!"
            )
        self.paths = paths
        self.path_hash_prefixes = path_hash_prefixes

    @classmethod
    def from_dict(cls, role_dict: Mapping[str, Any]) -> "Role":
        """Creates DelegatedRole object from its dict representation."""
        name = role_dict.pop("name")
        keyids = role_dict.pop("keyids")
        threshold = role_dict.pop("threshold")
        terminating = role_dict.pop("terminating")
        paths = role_dict.pop("paths", None)
        path_hash_prefixes = role_dict.pop("path_hash_prefixes", None)
        # All fields left in the role_dict are unrecognized.
        return cls(
            name,
            keyids,
            threshold,
            terminating,
            paths,
            path_hash_prefixes,
            role_dict,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Returns the dict representation of self."""
        base_role_dict = super().to_dict()
        res_dict = {
            "name": self.name,
            "terminating": self.terminating,
            **base_role_dict,
        }
        if self.paths:
            res_dict["paths"] = self.paths
        elif self.path_hash_prefixes:
            res_dict["path_hash_prefixes"] = self.path_hash_prefixes
        return res_dict


class Delegations:
    """A container object storing information about all delegations.

    Attributes:
        keys: A dictionary of keyids and key objects containing information
            about the corresponding key.
        roles: A list of DelegatedRole instances containing information about
            all delegated roles.
        unrecognized_fields: Dictionary of all unrecognized fields.

    """

    def __init__(
        self,
        keys: Mapping[str, Key],
        roles: List[DelegatedRole],
        unrecognized_fields: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.keys = keys
        self.roles = roles
        self.unrecognized_fields = unrecognized_fields or {}

    @classmethod
    def from_dict(cls, delegations_dict: Dict[str, Any]) -> "Delegations":
        """Creates Delegations object from its dict representation."""
        keys = delegations_dict.pop("keys")
        keys_res = {}
        for keyid, key_dict in keys.items():
            keys_res[keyid] = Key.from_dict(key_dict)
        roles = delegations_dict.pop("roles")
        roles_res = []
        for role_dict in roles:
            new_role = DelegatedRole.from_dict(role_dict)
            roles_res.append(new_role)
        # All fields left in the delegations_dict are unrecognized.
        return cls(keys_res, roles_res, delegations_dict)

    def to_dict(self) -> Dict[str, Any]:
        """Returns the dict representation of self."""
        keys = {keyid: key.to_dict() for keyid, key in self.keys.items()}
        roles = [role_obj.to_dict() for role_obj in self.roles]
        return {
            "keys": keys,
            "roles": roles,
            **self.unrecognized_fields,
        }


class Targets(Signed):
    """A container for the signed part of targets metadata.

    Attributes:
        targets: A dictionary that contains information about target files::

            {
                '<TARGET FILE NAME>': {
                    'length': <TARGET FILE SIZE>,
                    'hashes': {
                        '<HASH ALGO 1>': '<TARGET FILE HASH 1>',
                        '<HASH ALGO 2>': '<TARGETS FILE HASH 2>',
                        ...
                    },
                    'custom': <CUSTOM OPAQUE DICT> // optional
                },
                ...
            }

        delegations: An optional object containing a list of delegated target
            roles and public key store used to verify their metadata
            signatures.

    """

    _signed_type = "targets"

    # TODO: determine an appropriate value for max-args and fix places where
    # we violate that. This __init__ function takes 7 arguments, whereas the
    # default max-args value for pylint is 5
    # pylint: disable=too-many-arguments
    def __init__(
        self,
        version: int,
        spec_version: str,
        expires: datetime,
        targets: Dict[str, Any],
        delegations: Optional[Delegations] = None,
        unrecognized_fields: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(version, spec_version, expires, unrecognized_fields)
        # TODO: Add class for meta
        self.targets = targets
        self.delegations = delegations

    @classmethod
    def from_dict(cls, targets_dict: Dict[str, Any]) -> "Targets":
        """Creates Targets object from its dict representation."""
        common_args = cls._common_fields_from_dict(targets_dict)
        targets = targets_dict.pop("targets")
        delegations = targets_dict.pop("delegations", None)
        if delegations:
            delegations = Delegations.from_dict(delegations)
        # All fields left in the targets_dict are unrecognized.
        return cls(*common_args, targets, delegations, targets_dict)

    def to_dict(self) -> Dict[str, Any]:
        """Returns the dict representation of self."""
        targets_dict = self._common_fields_to_dict()
        targets_dict["targets"] = self.targets
        if self.delegations:
            targets_dict["delegations"] = self.delegations.to_dict()
        return targets_dict

    # Modification.
    def update(self, filename: str, fileinfo: Dict[str, Any]) -> None:
        """Assigns passed target file info to meta dict."""
        self.targets[filename] = fileinfo
