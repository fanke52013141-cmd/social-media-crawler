# -*- coding: utf-8 -*-
# 实时采集计数：以 jsonl 落盘行为准（全平台统一，无需各平台埋点），
# 按 last_modify_ts 过滤出「本轮」已落盘条数，供进度卡实时展示。
# 落盘行即真实采集到的数据，与最终导出同源，比解析日志窗口更准确。
import glob
import json
import os
from typing import Dict

from tools.async_file_writer import data_dir_of


def live_counts(platform: str, since_ts: float, data_mode: str = "content") -> Dict[str, int]:
    """统计 since_ts 之后已落盘的帖子/评论条数。

    返回 {saved, comment}：
      - 帖子模式：saved = 本轮落盘帖子数；
      - 评论模式：comment = 本轮落盘评论数。
    只读最近修改过的 jsonl 文件，按行内 last_modify_ts（毫秒）过滤本轮；
    缺该字段的行保守计入。整体容错，绝不影响进度主流程。
    """
    result = {"saved": 0, "comment": 0}
    if not since_ts:
        return result
    base = os.path.join(os.getcwd(), "data", data_dir_of(platform), "jsonl")
    since_ms = int(since_ts * 1000)
    want_contents = data_mode != "comment"
    want_comments = data_mode == "comment"
    for kind, key, want in (("contents", "saved", want_contents),
                            ("comments", "comment", want_comments)):
        if not want:
            continue
        for f in glob.glob(os.path.join(base, f"*_{kind}_*.jsonl")):
            try:
                if os.path.getmtime(f) < since_ts:
                    continue
                with open(f, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except (ValueError, TypeError):
                            continue
                        lm = row.get("last_modify_ts")
                        try:
                            if lm is None or int(lm) >= since_ms:
                                result[key] += 1
                        except (ValueError, TypeError):
                            result[key] += 1
            except OSError:
                continue
    return result