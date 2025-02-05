#
# Copyright 2021 Ocean Protocol Foundation
# SPDX-License-Identifier: Apache-2.0
#
import json
import logging
import os
import time
from json import JSONDecodeError
from threading import Thread

import elasticsearch
from eth_account import Account
from eth_utils import is_address

from aquarius.app.auth_util import sanitize_addresses
from aquarius.app.util import get_bool_env_value
from aquarius.block_utils import BlockProcessingClass
from aquarius.events.constants import EVENT_METADATA_CREATED, EVENT_METADATA_UPDATED
from aquarius.events.processors import (
    MetadataCreatedProcessor,
    MetadataUpdatedProcessor,
)
from aquarius.events.purgatory import Purgatory
from aquarius.events.util import get_metadata_contract, get_metadata_start_block
from aquarius.app.es_instance import ElasticsearchInstance


logger = logging.getLogger(__name__)


class EventsMonitor(BlockProcessingClass):
    """Detect on-chain published Metadata and cache it in the database for
    fast retrieval and searchability.

    The published metadata is extracted from the `MetadataCreated`
    event log from the `Metadata` smartcontract. Metadata updates are also detected using
    the `MetadataUpdated` event.

    The Metadata json object is expected to be
    in an `lzma` compressed form. If desired the metadata can also be encrypted for specific
    use cases. When using encrypted Metadata, the EventsMonitor requires the private key of
    the ethereum account that is used for encryption. This can be specified in `EVENTS_ECIES_PRIVATE_KEY`
    envvar.

    The events monitor pauses for 25 seconds between updates.

    The cached Metadata can be restricted to only those published by specific ethereum accounts.
    To do this set the `ALLOWED_PUBLISHERS` envvar to the list of ethereum addresses of known publishers.



    """

    _instance = None

    def __init__(self, web3, config_file, metadata_contract=None):
        self._es_instance = ElasticsearchInstance(config_file)

        self._other_db_index = f"{self._es_instance.db_index}_plus"
        self._es_instance.es.indices.create(index=self._other_db_index, ignore=400)

        self._web3 = web3

        if not metadata_contract:
            metadata_contract = get_metadata_contract(self._web3)
        self._chain_id = self._web3.eth.chain_id
        self.add_chain_id_to_chains_list()
        self._index_name = "events_last_block_" + str(self._chain_id)
        self._contract = metadata_contract
        self._contract_address = self._contract.address if self._contract else None
        self._start_block = get_metadata_start_block()

        if get_bool_env_value("EVENTS_CLEAN_START", 0):
            self.reset_chain()

        self._ecies_private_key = os.getenv("EVENTS_ECIES_PRIVATE_KEY", "")
        self._ecies_account = None
        if self._ecies_private_key:
            self._ecies_account = Account.from_key(self._ecies_private_key)
        self._only_encrypted_ddo = get_bool_env_value("ONLY_ENCRYPTED_DDO", 0)

        self.get_or_set_last_block()
        allowed_publishers = set()
        try:
            publishers_str = os.getenv("ALLOWED_PUBLISHERS", "")
            allowed_publishers = (
                set(json.loads(publishers_str)) if publishers_str else set()
            )
        except (JSONDecodeError, TypeError, Exception) as e:
            logger.error(
                f"Reading list of allowed publishers failed: {e}\n"
                f"ALLOWED_PUBLISHERS is set to empty set."
            )

        self._allowed_publishers = set(sanitize_addresses(allowed_publishers))
        logger.debug(f"allowed publishers: {self._allowed_publishers}")

        logger.info(
            f"EventsMonitor: using Metadata contract address {self._contract_address} from block {self._start_block} on chain {self._chain_id}"
        )
        self._monitor_is_on = False
        default_sleep_time = 10
        try:
            self._monitor_sleep_time = int(
                os.getenv("OCN_EVENTS_MONITOR_QUITE_TIME", default_sleep_time)
            )
        except ValueError:
            self._monitor_sleep_time = default_sleep_time

        self._monitor_sleep_time = max(self._monitor_sleep_time, default_sleep_time)
        if not self._contract or not is_address(self._contract_address):
            logger.error(
                f"Contract address {self._contract_address} is not a valid address. Events thread not starting"
            )
            self._contract = None

        self.purgatory = (
            Purgatory(self._es_instance)
            if (os.getenv("ASSET_PURGATORY_URL") or os.getenv("ACCOUNT_PURGATORY_URL"))
            else None
        )

        purgatory_message = (
            "Enabling purgatory" if self.purgatory else "Purgatory is disabled"
        )
        logger.info("PURGATORY: " + purgatory_message)

    @property
    def block_envvar(self):
        return "METADATA_CONTRACT_BLOCK"

    def start_events_monitor(self):
        if self._monitor_is_on:
            return

        if self._contract_address is None:
            logger.error("Cannot start events monitor without a valid contract address")
            return

        if self._contract is None:
            logger.error("Cannot start events monitor without a valid contract object")
            return

        logger.info(
            f"Starting the events monitor on contract {self._contract_address}."
        )
        t = Thread(target=self.run_monitor, daemon=True)
        self._monitor_is_on = True
        t.start()

    def stop_monitor(self):
        self._monitor_is_on = False

    def run_monitor(self):
        while True:
            self.do_run_monitor()
            time.sleep(self._monitor_sleep_time)

    def do_run_monitor(self):
        if not self._monitor_is_on:
            return

        try:
            self.process_current_blocks()
        except (KeyError, Exception) as e:
            logger.error(f"Error processing event: {str(e)}.")

        if self.purgatory:
            try:
                self.purgatory.update_lists()
            except (KeyError, Exception) as e:
                logger.error(f"Error updating purgatory list: {str(e)}.")

    def process_current_blocks(self):
        """Process all blocks from the last processed block to the current block."""
        last_block = self.get_last_processed_block()
        current_block = self._web3.eth.block_number
        if (
            not current_block
            or not isinstance(current_block, int)
            or current_block <= last_block
        ):
            return

        from_block = last_block

        start_block_chunk = from_block
        for end_block_chunk in range(
            from_block, current_block, self.blockchain_chunk_size
        ):
            self.process_block_range(start_block_chunk, end_block_chunk)
            start_block_chunk = end_block_chunk

        # Process last few blocks because range(start, end) doesn't include end
        self.process_block_range(end_block_chunk, current_block)

    def process_block_range(self, from_block, to_block):
        """Process a range of blocks."""
        logger.debug(
            f"Metadata monitor (chain: {self._chain_id})>>>> from_block:{from_block}, current_block:{to_block} <<<<"
        )

        if from_block > to_block:
            return

        processor_args = [
            self._es_instance,
            self._web3,
            self._ecies_account,
            self._allowed_publishers,
            self.purgatory,
            self._chain_id,
        ]

        for event in self.get_event_logs(EVENT_METADATA_CREATED, from_block, to_block):
            try:
                event_processor = MetadataCreatedProcessor(*([event] + processor_args))
                event_processor.process()
            except Exception as e:
                logger.error(
                    f"Error processing create metadata event: {e}\n" f"event={event}"
                )

        for event in self.get_event_logs(EVENT_METADATA_UPDATED, from_block, to_block):
            try:
                event_processor = MetadataUpdatedProcessor(*([event] + processor_args))
                event_processor.process()
            except Exception as e:
                logger.error(
                    f"Error processing update metadata event: {e}\n" f"event={event}"
                )

        self.store_last_processed_block(to_block)

    def get_last_processed_block(self):
        block = 0
        try:
            last_block_record = self._es_instance.es.get(
                index=self._other_db_index, id=self._index_name, doc_type="_doc"
            )["_source"]
            block = last_block_record["last_block"]
        except Exception as e:
            logger.error(f"Cannot get last_block error={e}")
        # no need to start from 0 if we have a deployment block
        if block < self._start_block:
            block = self._start_block
        return block

    def store_last_processed_block(self, block):
        # make sure that we don't write a block < then needed
        stored_block = self.get_last_processed_block()
        if block <= stored_block:
            return
        record = {"last_block": block}
        try:
            self._es_instance.es.index(
                index=self._other_db_index,
                id=self._index_name,
                body=record,
                doc_type="_doc",
                refresh="wait_for",
            )["_id"]

        except elasticsearch.exceptions.RequestError:
            logger.error(
                f"store_last_processed_block: block={block} type={type(block)}, ES RequestError"
            )

    def add_chain_id_to_chains_list(self):
        try:
            chains = self._es_instance.es.get(
                index=self._other_db_index, id="chains", doc_type="_doc"
            )["_source"]
        except Exception:
            chains = dict()
        chains[str(self._chain_id)] = True

        try:
            self._es_instance.es.index(
                index=self._other_db_index,
                id="chains",
                body=json.dumps(chains),
                doc_type="_doc",
                refresh="wait_for",
            )["_id"]
            logger.info(f"Added {self._chain_id} to chains list")
        except elasticsearch.exceptions.RequestError:
            logger.error(
                f"Cannot add chain_id {self._chain_id} to chains list: ES RequestError"
            )

    def reset_chain(self):
        assets = self.get_assets_in_chain()
        for asset in assets:
            try:
                self._es_instance.delete(asset["id"])
            except Exception as e:
                logging.error(f"Delete asset failed: {str(e)}")

        self.store_last_processed_block(self._start_block)

    def get_assets_in_chain(self):
        body = {
            "query": {
                "query_string": {"query": self._chain_id, "default_field": "chainId"}
            }
        }
        page = self._es_instance.es.search(index=self._es_instance.db_index, body=body)
        total = page["hits"]["total"]
        body["size"] = total
        page = self._es_instance.es.search(index=self._es_instance.db_index, body=body)

        object_list = []
        for x in page["hits"]["hits"]:
            object_list.append(x["_source"])

        return object_list

    def get_event_logs(self, event_name, from_block, to_block, chunk_size=1000):
        if event_name not in [EVENT_METADATA_CREATED, EVENT_METADATA_UPDATED]:
            raise ValueError(f"Event name {event_name} not supported.")

        event = getattr(self._contract.events, event_name)

        _from = from_block
        _to = min(_from + chunk_size - 1, to_block)

        logger.info(
            f"Searching for {event_name} events on chain {self._chain_id} "
            f"in blocks {from_block} to {to_block}."
        )

        all_logs = []
        while _from <= to_block:
            # Search current chunk
            logs = event().getLogs(fromBlock=_from, toBlock=_to)
            all_logs.extend(logs)
            if (_from - from_block) % 1000 == 0:
                logger.debug(
                    f"Searched blocks {_from} to {_to} on chain {self._chain_id}"
                    f"{len(all_logs)} {event_name} events detected so far."
                )

            # Prepare for next chunk
            _from = _to + 1
            _to = min(_from + chunk_size - 1, to_block)

        logger.info(
            f"Finished searching for {event_name} events on chain {self._chain_id} "
            f"in blocks {from_block} to {to_block}. "
            f"{len(all_logs)} {event_name} events detected."
        )

        return all_logs
