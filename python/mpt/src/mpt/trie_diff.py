import json
import logging
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple, Union

from ethereum.cancun.fork_types import Account, Address
from ethereum.cancun.trie import BranchNode, ExtensionNode, InternalNode, LeafNode
from ethereum.crypto.hash import Hash32
from ethereum_rlp import rlp
from ethereum_rlp.rlp import Extended
from ethereum_types.bytes import Bytes, Bytes32
from ethereum_types.numeric import U256, Uint

from mpt.ethereum_tries import EthereumTrieTransitionDB
from mpt.utils import (
    check_branch_node,
    deserialize_to_internal_node,
    nibble_list_to_bytes,
)

logger = logging.getLogger(__name__)


@dataclass
class StateDiff:
    """
    Contains all information that is preserved between transactions.
    """

    _main_trie: Dict[Address, Tuple[Optional[Account], Optional[Account]]] = field(
        default_factory=dict
    )
    _storage_tries: Dict[
        Address, Tuple[Dict[Bytes32, Optional[U256]], Dict[Bytes32, Optional[U256]]]
    ] = field(default_factory=dict)

    # TODO: remove these from this class. They don't belong here. But it's useful to avoid passing them around.
    _nodes: Dict[Hash32, InternalNode] = field(default_factory=dict)
    _address_preimages: Dict[Hash32, Address] = field(default_factory=dict)
    _storage_key_preimages: Dict[Hash32, Bytes32] = field(default_factory=dict)

    @classmethod
    def from_json(cls, path: Path) -> "StateDiff":
        with open(path, "r") as f:
            data = json.load(f)
        return cls.from_data(data)

    @classmethod
    def from_data(cls, data: Dict[str, Any]) -> "StateDiff":
        """
        Parse state diff from ZKPI data.
        """
        state_diff = cls()
        for diff in data["extra"]["stateDiffs"]:
            address = Address.fromhex(diff["address"][2:])
            if "preAccount" in diff:
                pre_balance = U256(int(diff["preAccount"]["balance"][2:], 16))
                pre_nonce = Uint(int(diff["preAccount"]["nonce"][2:], 16))
                pre_code_hash = Hash32.fromhex(diff["preAccount"]["codeHash"][2:])
                pre_storage_hash = Hash32.fromhex(diff["preAccount"]["storageHash"][2:])
                # Explicitly instantiate without code, as it's not an interesting data in the
                # case of state / trie diffs
                pre_account = Account(
                    nonce=pre_nonce,
                    balance=pre_balance,
                    code_hash=pre_code_hash,
                    storage_root=pre_storage_hash,
                    code=None,
                )
            else:
                pre_account = None

            if "postAccount" in diff:
                post_balance = U256(int(diff["postAccount"]["balance"][2:], 16))
                post_nonce = Uint(int(diff["postAccount"]["nonce"][2:], 16))
                post_code_hash = Hash32.fromhex(diff["postAccount"]["codeHash"][2:])
                post_storage_hash = Hash32.fromhex(
                    diff["postAccount"]["storageHash"][2:]
                )
                post_account = Account(
                    nonce=post_nonce,
                    balance=post_balance,
                    code_hash=post_code_hash,
                    storage_root=post_storage_hash,
                    code=None,
                )
            else:
                post_account = None

            if "storage" in diff:
                for storage_diff in diff["storage"]:
                    key = Bytes32.fromhex(storage_diff["storageKey"][2:])
                    pre_int = int(storage_diff["preValue"][2:], 16)
                    post_int = int(storage_diff["postValue"][2:], 16)
                    pre = None if pre_int == 0 else U256(pre_int)
                    post = None if post_int == 0 else U256(post_int)
                    if address not in state_diff._storage_tries:
                        state_diff._storage_tries[address] = {}
                    state_diff._storage_tries[address][key] = tuple((pre, post))

            state_diff._main_trie[address] = tuple((pre_account, post_account))

        return state_diff

    @classmethod
    def from_tries(cls, tries: EthereumTrieTransitionDB) -> "StateDiff":
        diff = StateDiff(
            {}, {}, tries.nodes, tries.address_preimages, tries.storage_key_preimages
        )

        l_root = tries.state_root
        r_root = tries.post_state_root

        diff._compute_diff(l_root, r_root, Bytes(), diff._process_account_diff)
        return diff

    def _compute_diff(
        self,
        left: Optional[Hash32 | Bytes],
        right: Optional[Hash32 | Bytes],
        path: Bytes,
        process_leaf_diff: Callable,
    ):
        if left == right:
            return

        l_node = resolve(left, self._nodes)
        r_node = resolve(right, self._nodes)

        # Use direct class pattern matching
        match (l_node, r_node):
            case (None, None):
                # No change
                pass

            case (None, LeafNode()):
                # new leaf
                full_path = nibble_list_to_bytes(path + r_node.rest_of_key)
                process_leaf_diff(path=full_path, left=None, right=r_node)

            case (None, ExtensionNode()):
                # Look for diffs in the right sub-tree
                self._compute_diff(
                    None, r_node.subnode, path + r_node.key_segment, process_leaf_diff
                )

            case (None, BranchNode()):
                check_branch_node(r_node)
                # Look for diffs in all branches of the right sub-tree
                for i in range(0, 16):
                    self._compute_diff(
                        None,
                        r_node.subnodes[i],
                        path + bytes([i]),
                        process_leaf_diff,
                    )

            case (LeafNode(), None):
                # deleted leaf
                full_path = nibble_list_to_bytes(path + l_node.rest_of_key)
                process_leaf_diff(path=full_path, left=l_node, right=None)

            case (LeafNode(), LeafNode()):
                if l_node.rest_of_key == r_node.rest_of_key:
                    if l_node.value != r_node.value:
                        # Same path -> different values
                        full_path = nibble_list_to_bytes(path + l_node.rest_of_key)
                        return process_leaf_diff(
                            path=full_path, left=l_node, right=r_node
                        )
                    else:
                        # Same path -> same value -> no diff
                        return

                # Different paths -> delete old leaf, create new leaf
                path_left = nibble_list_to_bytes(path + l_node.rest_of_key)
                path_right = nibble_list_to_bytes(path + r_node.rest_of_key)

                process_leaf_diff(path=path_left, left=l_node, right=None)
                process_leaf_diff(path=path_right, left=None, right=r_node)
                return

            case (LeafNode(), ExtensionNode()):
                # Explore the extension node's subtree for any new leaves, comparing it to the old
                # leaf with the same key
                if l_node.rest_of_key.startswith(r_node.key_segment):
                    l_node = LeafNode(
                        l_node.rest_of_key[len(r_node.key_segment) :], l_node.value
                    )
                    return self._compute_diff(
                        l_node,
                        r_node.subnode,
                        path + r_node.key_segment,
                        process_leaf_diff,
                    )

                # Here we compute the deletion of the Leaf and creation of the ExtensionNode's children

                path_left = nibble_list_to_bytes(path + l_node.rest_of_key)
                process_leaf_diff(path=path_left, left=l_node, right=None)

                # we explore the right sub-tree
                self._compute_diff(
                    None, r_node.subnode, path + r_node.key_segment, process_leaf_diff
                )

            case (LeafNode(), BranchNode()):
                check_branch_node(r_node)
                # The branch was created and replaced the single leaf.
                # All branches - except the one whose first nibble matches the leaf's key - are new.
                for i in range(0, 16):
                    # we know that l_node.rest_of_key is not empty
                    # because it's a leaf node at the same height as a branch node
                    # leaf nodes with empty rest_of_key are only found at the bottom of the trie
                    # where branch nodes can't exist
                    if i != l_node.rest_of_key[0]:
                        self._compute_diff(
                            None,
                            r_node.subnodes[i],
                            path + bytes([i]),
                            process_leaf_diff,
                        )
                    else:
                        shortened_l_node = LeafNode(
                            l_node.rest_of_key[1:], l_node.value
                        )
                        self._compute_diff(
                            shortened_l_node,
                            r_node.subnodes[i],
                            path + bytes([i]),
                            process_leaf_diff,
                        )

            case (ExtensionNode(), None):
                # Look for diffs in the left sub-tree
                self._compute_diff(
                    l_node.subnode, None, path + l_node.key_segment, process_leaf_diff
                )

            case (ExtensionNode(), LeafNode()):
                # The extension node was deleted and replaced by a leaf - meaning that down the line of the extension node, in a branch, we deleted some nodes.
                # Explore the extension node's subtree for any deleted nodes, comparing it to the new leaf
                if r_node.rest_of_key.startswith(l_node.key_segment):
                    r_node = LeafNode(
                        r_node.rest_of_key[len(l_node.key_segment) :], r_node.value
                    )
                    self._compute_diff(
                        l_node.subnode,
                        r_node,
                        path + l_node.key_segment,
                        process_leaf_diff,
                    )
                    return

                # Here we compute the creation of a new leaf node and the deletion of the extension node's children
                path_right = nibble_list_to_bytes(path + r_node.rest_of_key)
                process_leaf_diff(path=path_right, left=None, right=r_node)

                # we explore the left sub-tree
                self._compute_diff(
                    l_node.subnode, None, path + l_node.key_segment, process_leaf_diff
                )

            case (ExtensionNode(), ExtensionNode()):
                # Equal keys -> Look for diffs in children
                if l_node.key_segment == r_node.key_segment:
                    self._compute_diff(
                        l_node.subnode,
                        r_node.subnode,
                        path + l_node.key_segment,
                        process_leaf_diff,
                    )
                # Right is prefix of left
                elif l_node.key_segment.startswith(r_node.key_segment):
                    # Compare the right node's value with the left node shortened by right key
                    l_node_shortened = ExtensionNode(
                        key_segment=Bytes(
                            l_node.key_segment[len(r_node.key_segment) :]
                        ),
                        subnode=l_node.subnode,
                    )
                    self._compute_diff(
                        l_node_shortened,
                        r_node.subnode,
                        path + r_node.key_segment,
                        process_leaf_diff,
                    )
                # Left is prefix of right
                elif r_node.key_segment.startswith(l_node.key_segment):
                    # Compare the left node's value with the right node shortened by left key
                    r_node_shortened = ExtensionNode(
                        key_segment=Bytes(
                            r_node.key_segment[len(l_node.key_segment) :]
                        ),
                        subnode=r_node.subnode,
                    )
                    self._compute_diff(
                        l_node.subnode,
                        r_node_shortened,
                        path + l_node.key_segment,
                        process_leaf_diff,
                    )
                # Both are different -> Look for diffs in both sub-trees
                else:
                    self._compute_diff(
                        l_node.subnode,
                        None,
                        path + l_node.key_segment,
                        process_leaf_diff,
                    )
                    self._compute_diff(
                        None,
                        r_node.subnode,
                        path + r_node.key_segment,
                        process_leaf_diff,
                    )

            case (ExtensionNode(), BranchNode()):
                check_branch_node(r_node)
                # Match on the corresponding nibble of the extension key segment
                for i in range(0, 16):
                    nibble = bytes([i])
                    # we know that l_node.key_segment is not empty
                    # as extension nodes key_segment len is at least 1
                    if l_node.key_segment[0] == nibble:
                        if len(l_node.key_segment) == 1:
                            # Fully consumed by this nibble: compare to the subnode
                            l_node_to_compare = l_node.subnode
                        else:
                            l_node_to_compare = ExtensionNode(
                                key_segment=Bytes(l_node.key_segment[1:]),
                                subnode=l_node.subnode,
                            )
                        # Remove the nibble from the extension key segment
                        self._compute_diff(
                            l_node_to_compare,
                            r_node.subnodes[i],
                            path + nibble,
                            process_leaf_diff,
                        )
                    else:
                        # Look for diffs in other branches
                        self._compute_diff(
                            None,
                            r_node.subnodes[i],
                            path + nibble,
                            process_leaf_diff,
                        )

            case (BranchNode(), None):
                check_branch_node(l_node)
                # Look for diffs in all branches of the left sub-tree
                for i in range(0, 16):
                    self._compute_diff(
                        l_node.subnodes[i],
                        None,
                        path + bytes([i]),
                        process_leaf_diff,
                    )

            case (BranchNode(), LeafNode()):
                check_branch_node(r_node)
                # The branch was deleted and replaced by a single leaf.
                # All branches - except the one whose first nibble matches the leaf's key - are deleted.
                # The remaining branch is compared to the leaf.
                for i in range(0, 16):
                    # we know that r_node.rest_of_key is not empty
                    # because it's a leaf node at the same height as a branch node
                    # leaf nodes with empty rest_of_key are only found at the bottom of the trie
                    # where branch nodes can't exist
                    if i != r_node.rest_of_key[0]:
                        self._compute_diff(
                            l_node.subnodes[i],
                            None,
                            path + bytes([i]),
                            process_leaf_diff,
                        )
                    else:
                        shortened_r_node = LeafNode(
                            r_node.rest_of_key[1:], r_node.value
                        )
                        self._compute_diff(
                            l_node.subnodes[i],
                            shortened_r_node,
                            path + bytes([i]),
                            process_leaf_diff,
                        )

            case (BranchNode(), ExtensionNode()):
                check_branch_node(r_node)
                # Match on the corresponding nibble of the extension key segment
                for i in range(0, 16):
                    nibble = bytes([i])
                    # we know that r_node.key_segment is not empty
                    # as extension nodes key_segment len is at least 1
                    if r_node.key_segment[0] == nibble:
                        # Remove the nibble from the extension key segment
                        r_node.key_segment = r_node.key_segment[1:]
                        self._compute_diff(
                            l_node.subnodes[i],
                            r_node.subnode,
                            path + nibble,
                            process_leaf_diff,
                        )
                    else:
                        # Look for diffs in other branches
                        self._compute_diff(
                            l_node.subnodes[i], None, path + nibble, process_leaf_diff
                        )

            case (BranchNode(), BranchNode()):
                check_branch_node(l_node)
                check_branch_node(r_node)
                # Look for diffs in all branches of the right sub-tree
                for i in range(0, 16):
                    l_subnode = l_node.subnodes[i]
                    r_subnode = r_node.subnodes[i]
                    self._compute_diff(
                        l_subnode,
                        r_subnode,
                        path + bytes([i]),
                        process_leaf_diff,
                    )

            case _:
                raise ValueError(
                    f"Node types do not match: {type(l_node)} != {type(r_node)}"
                )

    def _process_account_diff(
        self, path: Bytes32, left: Optional[LeafNode], right: Optional[LeafNode]
    ):
        address = self._address_preimages[path]
        left_account = None if left is None else Account.from_rlp(left.value)
        right_account = None if right is None else Account.from_rlp(right.value)

        self._main_trie[address] = (left_account, right_account)

        left_storage_root = None if left_account is None else left_account.storage_root
        right_storage_root = (
            None if right_account is None else right_account.storage_root
        )

        self._compute_diff(
            left_storage_root,
            right_storage_root,
            b"",
            partial(self._process_storage_diff, address=address),
        )

    def _process_storage_diff(
        self,
        address: Address,
        path: Bytes32,
        left: Optional[LeafNode],
        right: Optional[LeafNode],
    ):
        key = self._storage_key_preimages[path]
        left_decoded = (
            U256(int.from_bytes(rlp.decode(left.value), "big")) if left else None
        )
        right_decoded = (
            U256(int.from_bytes(rlp.decode(right.value), "big")) if right else None
        )
        if left is None:
            if address not in self._storage_tries:
                self._storage_tries[address] = {}
            self._storage_tries[address][key] = tuple((None, right_decoded))
        elif right is None:
            if address not in self._storage_tries:
                self._storage_tries[address] = {}
            self._storage_tries[address][key] = tuple((left_decoded, None))
        else:
            if address not in self._storage_tries:
                self._storage_tries[address] = {}
            self._storage_tries[address][key] = tuple((left_decoded, right_decoded))


def resolve(
    node: Optional[Union[InternalNode, Extended]], nodes: Dict[Hash32, InternalNode]
) -> InternalNode | None:
    if node is None or node == b"":
        return None
    if isinstance(node, InternalNode):
        return node
    if isinstance(node, bytes) and len(node) == 32:
        return nodes.get(node)
    if isinstance(node, list):
        return deserialize_to_internal_node(node)
    raise ValueError(f"Invalid node type: {type(node)}")
