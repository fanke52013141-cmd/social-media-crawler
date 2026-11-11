# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tools/async_file_writer.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#
# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

import asyncio
import csv
import json
import os
import pathlib
from typing import Dict, List
import aiofiles
import config
from tools.utils import utils
from tools.words import AsyncWordCloudGenerator

# 平台代码 -> 数据落盘目录名（各平台 store 初始化 AsyncFileWriter 时实际传入的 platform 值）。
# 注意：抖音/微博的目录名是全名（douyin/weibo），与平台代码（dy/wb）不一致。
PLATFORM_DATA_DIR = {
    "xhs": "xhs",
    "dy": "douyin",
    "wb": "weibo",
    "ks": "kuaishou",
    "bili": "bili",
    "tieba": "tieba",
    "zhihu": "zhihu",
    "toutiao": "toutiao",
}


def data_dir_of(platform: str) -> str:
    """返回平台代码对应的 jsonl 数据目录名；未登记时回退为代码本身。"""
    return PLATFORM_DATA_DIR.get(platform, platform)


class AsyncFileWriter:
    # jsonl 主键字段：用于按主键去重，避免重复采集同一帖子时产生重复行
    _JSONL_KEY_FIELD = {"comments": "comment_id", "contents": "note_id"}

    def __init__(self, platform: str, crawler_type: str):
        self.lock = asyncio.Lock()
        self.platform = platform
        self.crawler_type = crawler_type
        self.wordcloud_generator = AsyncWordCloudGenerator() if config.ENABLE_GET_WORDCLOUD else None
        # file_path -> 已写入主键集合（首次写入时从文件加载历史主键，兼顾跨进程/跨次重复）
        self._seen_keys = {}

    @staticmethod
    def _load_existing_keys(file_path: str, key_field: str) -> set:
        """读取目标 jsonl 文件中已存在的主键集合。"""
        keys = set()
        if not os.path.exists(file_path):
            return keys
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        pk = json.loads(line).get(key_field)
                        if pk is not None:
                            keys.add(pk)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
        return keys

    def _get_file_path(self, file_type: str, item_type: str) -> str:
        if config.SAVE_DATA_PATH:
            base_path = f"{config.SAVE_DATA_PATH}/{self.platform}/{file_type}"
        else:
            base_path = f"data/{self.platform}/{file_type}"
        pathlib.Path(base_path).mkdir(parents=True, exist_ok=True)
        file_name = f"{self.crawler_type}_{item_type}_{utils.get_current_date()}.{file_type}"
        return f"{base_path}/{file_name}"

    async def write_to_csv(self, item: Dict, item_type: str):
        file_path = self._get_file_path('csv', item_type)
        async with self.lock:
            file_exists = os.path.exists(file_path)
            async with aiofiles.open(file_path, 'a', newline='', encoding='utf-8-sig') as f:
                writer = csv.DictWriter(f, fieldnames=item.keys())
                if not file_exists or await f.tell() == 0:
                    await writer.writeheader()
                await writer.writerow(item)

    async def write_to_jsonl(self, item: Dict, item_type: str):
        file_path = self._get_file_path('jsonl', item_type)
        key_field = self._JSONL_KEY_FIELD.get(item_type)
        async with self.lock:
            if key_field is not None:
                if file_path not in self._seen_keys:
                    # 首次写入该文件：加载历史主键，防止与此前采集的记录重复
                    self._seen_keys[file_path] = await asyncio.to_thread(
                        self._load_existing_keys, file_path, key_field)
                pk = item.get(key_field)
                if pk is not None:
                    if pk in self._seen_keys[file_path]:
                        return  # 主键已存在，跳过追加，避免重复行
                    self._seen_keys[file_path].add(pk)
            async with aiofiles.open(file_path, 'a', encoding='utf-8') as f:
                await f.write(json.dumps(item, ensure_ascii=False) + '\n')

    async def write_single_item_to_json(self, item: Dict, item_type: str):
        file_path = self._get_file_path('json', item_type)
        async with self.lock:
            existing_data = []
            if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
                    try:
                        content = await f.read()
                        if content:
                            existing_data = json.loads(content)
                        if not isinstance(existing_data, list):
                            existing_data = [existing_data]
                    except json.JSONDecodeError:
                        existing_data = []

            existing_data.append(item)

            async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(existing_data, ensure_ascii=False, indent=4))

    async def generate_wordcloud_from_comments(self):
        """
        Generate wordcloud from comments data
        Only works when ENABLE_GET_WORDCLOUD and ENABLE_GET_COMMENTS are True
        """
        if not config.ENABLE_GET_WORDCLOUD or not config.ENABLE_GET_COMMENTS:
            return

        if not self.wordcloud_generator:
            return

        try:
            # Read comments from JSON or JSONL file
            comments_data = []
            jsonl_file_path = self._get_file_path('jsonl', 'comments')
            json_file_path = self._get_file_path('json', 'comments')

            if os.path.exists(jsonl_file_path) and os.path.getsize(jsonl_file_path) > 0:
                async with aiofiles.open(jsonl_file_path, 'r', encoding='utf-8') as f:
                    async for line in f:
                        line = line.strip()
                        if line:
                            try:
                                comments_data.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue
            elif os.path.exists(json_file_path) and os.path.getsize(json_file_path) > 0:
                async with aiofiles.open(json_file_path, 'r', encoding='utf-8') as f:
                    content = await f.read()
                    if content:
                        comments_data = json.loads(content)
                        if not isinstance(comments_data, list):
                            comments_data = [comments_data]

            if not comments_data:
                utils.logger.info(f"[AsyncFileWriter.generate_wordcloud_from_comments] No comments data found")
                return

            # Filter comments data to only include 'content' field
            # Handle different comment data structures across platforms
            filtered_data = []
            for comment in comments_data:
                if isinstance(comment, dict):
                    # Try different possible content field names
                    content_text = comment.get('content') or comment.get('comment_text') or comment.get('text') or ''
                    if content_text:
                        filtered_data.append({'content': content_text})

            if not filtered_data:
                utils.logger.info(f"[AsyncFileWriter.generate_wordcloud_from_comments] No valid comment content found")
                return

            # Generate wordcloud
            if config.SAVE_DATA_PATH:
                words_base_path = f"{config.SAVE_DATA_PATH}/{self.platform}/words"
            else:
                words_base_path = f"data/{self.platform}/words"
            pathlib.Path(words_base_path).mkdir(parents=True, exist_ok=True)
            words_file_prefix = f"{words_base_path}/{self.crawler_type}_comments_{utils.get_current_date()}"

            utils.logger.info(f"[AsyncFileWriter.generate_wordcloud_from_comments] Generating wordcloud from {len(filtered_data)} comments")
            await self.wordcloud_generator.generate_word_frequency_and_cloud(filtered_data, words_file_prefix)
            utils.logger.info(f"[AsyncFileWriter.generate_wordcloud_from_comments] Wordcloud generated successfully at {words_file_prefix}")

        except Exception as e:
            utils.logger.error(f"[AsyncFileWriter.generate_wordcloud_from_comments] Error generating wordcloud: {e}")
