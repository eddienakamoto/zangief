import os
import asyncio
import concurrent.futures
import re
import time
from functools import partial
import numpy as np
import random
import argparse
from typing import cast, Any
from datetime import datetime
import copy

from communex.client import CommuneClient
from communex.module.client import ModuleClient
from communex._common import get_node_url
from communex.compat.key import classic_load_key
from communex.module.module import Module
from communex.types import Ss58Address
from communex.misc import get_map_modules
from substrateinterface import Keypair
from weights_io import ensure_weights_file, write_weight_file, read_weight_file
from sigmoid import sigmoid_rewards

from config import Config
from loguru import logger


from reward import Reward
from prompt_datasets.cc_100 import CC100


logger.add("logs/log_{time:YYYY-MM-DD}.log", rotation="1 day", level="INFO")

IP_REGEX = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+")


def extract_address(string: str):
    """
    Extracts an address from a string.
    """
    return re.search(IP_REGEX, string)


def get_miner_ip_port(client: CommuneClient, netuid: int, balances=False):
    modules = cast(dict[str, Any], get_map_modules(
    client, netuid=netuid, include_balances=balances))

    # Convert the values to a human readable format
    modules_to_list = [value for _, value in modules.items()]

    miners: list[Any] = []

    for module in modules_to_list:
        if module["incentive"] == module["dividends"] == 0:
            miners.append(module)
        elif module["incentive"] > module["dividends"]:
            miners.append(module)

    return miners 


def get_ip_port(modules_adresses: dict[int, str]):
    """
    Get the IP and port information from module addresses.

    Args:
        modules_addresses: A dictionary mapping module IDs to their addresses.

    Returns:
        A dictionary mapping module IDs to their IP and port information.
    """

    filtered_addr = {id: extract_address(addr) for id, addr in modules_adresses.items()}
    ip_port = {
        id: x.group(0).split(":") if x is not None else ["0.0.0.0", "00"] for id, x in filtered_addr.items()
    }

    return ip_port


def get_netuid(is_testnet):
    if is_testnet:
        return 23
    else:
        return 1


def normalize_scores(scores, scale=False):
    min_score = min(scores)
    max_score = max(scores)

    if min_score == max_score:
        # If all scores are the same, give all ones
        return [1] * len(scores)

    # Normalize scores from 0 to 1
    normalized_scores = [(score - min_score) / (max_score - min_score) for score in scores]

    # Scale normalized scores from min_score to 1
    if scale:
        normalized_scores = [score * (1 - min_score) + min_score for score in normalized_scores]
    return normalized_scores


class TranslateValidator(Module):
    """
    A class for validating text generated by modules in a subnet.

    Attributes:
        client: The CommuneClient instance used to interact with the subnet.
        key: The keypair used for authentication.
        netuid: The unique identifier of the subnet.
        call_timeout: The timeout value for module calls in seconds (default: 60).

    Methods:
        get_modules: Retrieve all module addresses from the subnet.
        _get_miner_prediction: Prompt a miner module to generate an answer to the given question.
        _score_miner: Score the generated answer against the validator's own answer.
        get_miner_prompt: Generate a prompt for the miner modules.
        validate_step: Perform a validation step by generating questions, prompting modules, and scoring answers.
        validation_loop: Run the validation loop continuously based on the provided settings.
    """

    def __init__(
        self,
        key: Keypair,
        netuid: int,
        client: CommuneClient,
        call_timeout: int = 20,
        use_testnet: bool = False,
    ) -> None:
        super().__init__()
        self.client = client
        self.key = key
        self.netuid = netuid
        self.call_timeout = call_timeout
        self.use_testnet = use_testnet
        self.uid = None
        home_dir = os.path.expanduser("~")
        commune_dir = os.path.join(home_dir, ".commune")
        self.zangief_dir = os.path.join(commune_dir, "zangief")
        self.weights_file = os.path.join(self.zangief_dir, "weights.json")
        ensure_weights_file(zangief_dir_name=self.zangief_dir, weights_file_name=self.weights_file)
        write_weight_file(self.weights_file, {})

        self.reward = Reward()
        self.languages = [
            "ar",
            "de",
            "en",
            "es",
            "fa",
            "fr",
            "hi",
            "he",
            "pt",
            "ru",
            "ur",
            "vi",
            "zh"
        ]
        cc_100 = CC100()
        self.datasets = {
            "ar": [cc_100],
            "de": [cc_100],
            "en": [cc_100],
            "es": [cc_100],
            "fa": [cc_100],
            "fr": [cc_100],
            "hi": [cc_100],
            "he": [cc_100],
            "pt": [cc_100],
            "ru": [cc_100],
            "ur": [cc_100],
            "vi": [cc_100],
            "zh": [cc_100],
        }

    def get_addresses(self, client: CommuneClient, netuid: int) -> dict[int, str]:
        """
        Retrieve all module addresses from the subnet.

        Args:
            client: The CommuneClient instance used to query the subnet.
            netuid: The unique identifier of the subnet.

        Returns:
            A dictionary mapping module IDs to their addresses.
        """
        module_addresses = client.query_map_address(netuid)
        return module_addresses
    
    def split_ip_port(self, ip_port):
        # Check if the input is empty or None
        if not ip_port:
            return None, None
        
        # Split the input string by the colon
        parts = ip_port.split(":")

        logger.info(f"PARTS: {parts}")
        
        # Check if the split resulted in exactly two parts
        if len(parts) == 2:
            ip, port = parts
            logger.info(f"IP: {ip}")
            logger.info(f"PORT: {port}")
            return ip, port
        else:
            return None, None

    def _get_miner_prediction(
        self,
        prompt: str,
        miner_info: tuple[list[str], Ss58Address],
    ) -> str | None:
        """
        Prompt a miner module to generate an answer to the given question.

        Args:
            question: The question to ask the miner module.
            miner_info: A tuple containing the miner's connection information and key.

        Returns:
            The generated answer from the miner module, or None if the miner fails to generate an answer.
        """
        question, source_language, target_language = prompt
        connection = miner_info['address']
        miner_key = miner_info['key']
        # connection, miner_key = miner_info
        module_ip, module_port = self.split_ip_port(connection)
        
        if module_ip == "None" or module_port == "None":
            return ""

        client = ModuleClient(module_ip, int(module_port), self.key)

        try:
            miner_answer = asyncio.run(
                client.call(
                    "generate",
                    miner_key,
                    {"prompt": question, "source_language": source_language, "target_language": target_language},
                    timeout=self.call_timeout,
                )
            )
            miner_answer = miner_answer["answer"]
        except Exception as e:
            logger.error(f"Error getting miner response: {e}")
            miner_answer = None
        return miner_answer

    # def get_miners_to_query(self, miner_keys):
    #     return miner_keys

    def get_miners_to_query(self, miners: list[dict[str, Any]]):
        # TODO: Clean this up to be more manageable 

        scored_miners = read_weight_file(self.weights_file)
        remaining_miners = copy.deepcopy(miners)
        miners_to_query = []
        counter = 0

        logger.info(f"SCORED_MINERSa: {scored_miners}")

        for i, m in enumerate(miners):
            if str(m['uid']) in scored_miners:
                if m['key'] != scored_miners[str(m['uid'])]['ss58']:
                    current_weights = read_weight_file(self.weights_file)
                    if str(m['uid']) in current_weights:
                        del current_weights[str(m['uid'])]
                        write_weight_file(self.weights_file, current_weights)

                if m['key'] == scored_miners[str(m['uid'])]['ss58']:
                    remaining_miners = [rm for rm in remaining_miners if rm['uid'] != m['uid']]
                    continue

            miners_to_query.append(m)
            counter += 1

            if counter == 8:
                break 

        logger.info(f"SCORED_MINERSb: {scored_miners}")
        logger.info(f"MINERS_TO_QUERY: {miners_to_query}")

        return remaining_miners, miners_to_query

    def get_miner_prompt(self) -> tuple:
        """
        Generate a prompt for the miner modules.

        Returns:
            The generated prompt for the miner modules.
        """
        source_language = np.random.choice(self.languages).item()
        target_languages = [language for language in self.languages if language != source_language]
        target_language = np.random.choice(target_languages).item()

        source_datasets = self.datasets[source_language]
        random_dataset_index = random.randint(0, len(source_datasets) - 1)
        source_dataset = source_datasets[random_dataset_index]

        source_text = source_dataset.get_random_record(source_language)
        return source_text, source_language, target_language

    async def validate_step(
        self, netuid: int
    ) -> None:
        """
        Perform a validation step.

        Generates questions based on the provided settings, prompts modules to generate answers,
        and scores the generated answers against the validator's own answers.

        Args:
            netuid: The network UID of the subnet.
        """
        # try:
        #     modules_addresses = self.get_addresses(self.client, netuid)
        # except Exception as e:
        #     logger.error(f"Error syncing with the network: {e}")
        #     self.client = CommuneClient(get_node_url())
        #     modules_addresses = self.get_addresses(self.client, netuid)
        # self.uid = None 

        miners = get_miner_ip_port(self.client, self.netuid)

        modules_keys = self.client.query_map_key(netuid)
        val_ss58 = self.key.ss58_address
        if val_ss58 not in modules_keys.values():
            logger.error(f"Validator key {val_ss58} is not registered in subnet")
            return None
        
        for uid, ss58 in modules_keys.items():
            if ss58.__str__() == val_ss58:
                self.uid = uid
                logger.info(f"UID IS !!!!!!! {self.uid}")

        # miners_to_query = self.get_miners_to_query(modules_keys.keys())
        remaining_miners, miners_to_query = self.get_miners_to_query(miners)

        # modules_info: dict[int, tuple[list[str], Ss58Address]] = {}
        # miner_uids = []
        # modules_filtered_address = get_ip_port(modules_addresses)
        # for module_id in miners_to_query:
        #     module_addr = modules_filtered_address.get(module_id, None)
        #     if not module_addr:
        #         continue
        #     modules_info[module_id] = (module_addr, modules_keys[module_id])
        #     miner_uids.append(module_id)

        # score_dict: dict[int, float] = {}

        miner_prompt, source_language, target_language = self.get_miner_prompt()

        logger.debug("Source")
        logger.debug(source_language)
        logger.debug("Target")
        logger.debug(target_language)
        logger.debug("Prompt")
        logger.debug(miner_prompt)

        prompt = (miner_prompt, source_language, target_language)
        get_miner_prediction = partial(self._get_miner_prediction, prompt)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            it = executor.map(get_miner_prediction, miners_to_query)
            miner_answers = [*it]

        scores = self.reward.get_scores(miner_prompt, target_language, miner_answers)

        logger.debug("Miner prompt")
        logger.debug(miner_prompt)
        logger.debug("Miner answers")
        logger.debug(miner_answers)
        logger.debug("Raw scores")
        logger.debug(scores)

        score_dict: dict[int, float] = {}
        for uid, score in zip([m['uid'] for m in miners_to_query], scores):
            score_dict[uid] = score

        data_to_write = {}
        logger.info(f"SCORE DICT: {score_dict}")
        for item in miners_to_query:
            ss58 = item['key']
            uid = int(item['uid'])
            score = score_dict[uid]
            data_to_write[uid] = {"ss58": ss58, "score": score}

        current_weights = read_weight_file(self.weights_file)
        for key, data in data_to_write.items():
            current_weights[key] = data

        write_weight_file(self.weights_file, current_weights)
        ddd = read_weight_file(self.weights_file)
        logger.info(f"READ DATA: {ddd}")

        logger.info("Miner UIDs")
        logger.info([m['uid'] for m in miners_to_query])
        logger.info("Final scores")
        logger.info(scores)

        # if not score_dict:
        #     logger.info("No miner returned a valid answer")
        #     return None

        if len(remaining_miners) == 0:
            scores = read_weight_file(self.weights_file)

            s_dict: dict[int: float] = {}
            for uid, data in scores.items():
                # s_dict[int(uid)] = data['score']
                s_dict[uid] = data['score']

            logger.info("SETTING WEIGHTS")
            self.set_weights(s_dict)
            write_weight_file(self.weights_file, {})

    def validation_loop(self, config: Config | None = None) -> None:
        while True:
            logger.info("Begin validator step ... ")
            asyncio.run(self.validate_step(self.netuid))

            interval = int(config.validator.get("interval"))
            logger.info(f"Sleeping for {interval} seconds ... ")
            time.sleep(interval)

    def set_weights(self, s_dict):
        """
        Set weights for miners based on their normalized, scaled and sigmoided scores.
        """
        full_score_dict = s_dict
        weighted_scores: dict[int: float] = {}

        abnormal_scores = full_score_dict.values()
        normal_scores = normalize_scores(abnormal_scores)
        # normal_scores = abnormal_scores
        score_dict = {uid: score for uid, score in zip(full_score_dict.keys(), normal_scores)}
        sigmoided_scores = sigmoid_rewards(score_dict)
        scores = sum(sigmoided_scores.values())

        for uid, score in sigmoided_scores.items():
            weight = score * 1000 / scores
            weighted_scores[uid] = weight

        weighted_scores = {k: v for k, v in zip(weighted_scores.keys(), normalize_scores(weighted_scores.values())) if v != 0}

        if self.uid is not None and self.uid in weighted_scores:
            del weighted_scores[self.uid]
            logger.info(f"REMOVING UID !!!!!! {self.uid}")
        else:
            logger.info("NOT REMOVING ANY UID")

        uids = list(weighted_scores.keys())
        weights = list(weighted_scores.values())

        logger.info(f"WEIGHTS TO SET: {weighted_scores}")

        try:
            self.client.vote(key=self.key, uids=uids, weights=weights, netuid=self.netuid)
        except Exception as e:
            logger.error(f"WARNING: Failed to set weights with exception: {e}. Will retry.")
            sleepy_time = random.uniform(1, 2)
            time.sleep(sleepy_time)
            # retry with a different node
            self.client = CommuneClient(get_node_url(use_testnet=self.use_testnet))
            self.client.vote(key=self.key, uids=uids, weights=weights, netuid=self.netuid)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="transaction validator")
    parser.add_argument("--config", type=str, default=None, help="config file path")
    args = parser.parse_args()

    logger.info("Loading validator config ... ")
    if args.config is None:
        default_config_path = 'env/config.ini'
        config_file = default_config_path
    else:
        config_file = args.config
    config = Config(config_file=config_file)

    use_testnet = True if config.validator.get("testnet") == "1" else False
    if use_testnet:
        logger.info("Connecting to TEST network ... ")
    else:
        logger.info("Connecting to Main network ... ")
    c_client = CommuneClient(get_node_url(use_testnet=use_testnet))
    subnet_uid = get_netuid(use_testnet)
    keypair = classic_load_key(config.validator.get("keyfile"))

    validator = TranslateValidator(
        keypair,
        subnet_uid,
        c_client,
        call_timeout=20,
        use_testnet=use_testnet
    )
    logger.info("Running validator ... ")
    validator.validation_loop(config)