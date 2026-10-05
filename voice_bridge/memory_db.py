# -*- coding: utf-8 -*-
"""小紫记忆数据库 - 长期记忆 + 用户画像 + 会话记录（含时间字段）

SQLite 存储（零第三方依赖，Python 内建 sqlite3）：
  facts#        用户画像事实（随时间累积 last_seen_at / seen_count）
  conversations#会话逐轮记录（含 created_at 时间戳）

用法：
  db = MemoryDB(DB_PATH)
  db.record_turn(user_text, assistant_reply)   # 会话入库 + 自动提取事实
  db.build_prompt(base_prompt, now_str)        # 注入时间+事实+会话摘要
"""
import json
import os
import re
import sqlite3
import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "conf", "memory.db")


class MemoryDB:
    def __init__(self, path=None):
        self.path = path or DB_PATH
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self):
        cur = self.conn
        cur.execute("""
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                seen_count INTEGER DEFAULT 1
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_conv_time ON conversations(created_at)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_fact_uniq ON facts(content)")
        self.conn.commit()

    # ---- 时间 ----
    @staticmethod
    def _now():
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ---- 事实提取（第一人称口语模式 + 短句）----
    _FACT_PATTERNS = [
        (r"(?:我|人家|偶)(?:喜欢|最爱|超爱|特别爱|可喜欢)(.{1,30})", "preference"),
        (r"(?:我|人家|偶)(?:讨厌|不喜欢|最恨|怕|害怕)(.{1,30})", "aversion"),
        (r"(?:我是|我叫|我的名字是)(.{1,20})", "identity"),
        (r"(?:我(?:住在|家在|生活在))(.{1,30})", "location"),
        (r"(?:我是(?:一个|一只|一名|个)?)(.{1,20})(?:的)?(?:学生|老师|程序员|工程师|设计师|医生|护士|打工人)", "occupation"),
        (r"我(?:正在|在)(?:做|学|写|研究|玩|看|听|吃)(.{1,25})", "activity"),
        (r"我(?:今天|刚才|昨天)(.{1,30})", "event"),
    ]

    @classmethod
    def extract_facts(cls, text):
        """从用户话语提取事实，返回 [(category, content)]。"""
        out = []
        if not text or len(text) > 80:
            return out
        for pattern, cat in cls._FACT_PATTERNS:
            m = re.search(pattern, text)
            if m:
                content = m.group(1).strip(" ，。！？~的")
                if content and len(content) >= 2:
                    out.append((cat, content[:40]))
        return out

    def save_facts(self, facts):
        """去重入库：已存在则更新 last_seen_at + seen_count。"""
        if not facts:
            return
        now = self._now()
        for cat, content in facts:
            self.conn.execute(
                "INSERT INTO facts(category, content, last_seen_at, seen_count) VALUES(?,?,?,1) "
                "ON CONFLICT(content) DO UPDATE SET last_seen_at=?, seen_count=seen_count+1",
                (cat, content, now, now))
        self.conn.commit()

    def save_turn(self, role, content):
        self.conn.execute(
            "INSERT INTO conversations(role, content, created_at) VALUES(?,?,?)",
            (role, content, self._now()))
        self.conn.commit()

    def record_turn(self, user_text, assistant_reply):
        self.save_turn("user", user_text)
        self.save_turn("assistant", assistant_reply)
        self.save_facts(self.extract_facts(user_text))

    # ---- 记忆摘取 ----
    def recent_facts(self, limit=15):
        """列出记忆最深刻/最新的事实。"""
        rows = self.conn.execute(
            "SELECT category, content, last_seen_at, seen_count "
            "FROM facts ORDER BY seen_count DESC, last_seen_at DESC LIMIT ?",
            (limit,)).fetchall()
        return rows

    def recent_conversation(self, limit=8):
        """最近若干轮对话（用于短程上下文补足）。"""
        rows = self.conn.execute(
            "SELECT role, content FROM conversations "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        # 倒回时间序
        return list(reversed(rows))

    def build_prompt(self, base_prompt, inject_time=True, max_facts=15, max_recent=8):
        """在人设 prompt 后追加：当前时间 + 用户画像 + 近期对话回忆。"""
        parts = [base_prompt] if base_prompt else []
        extra = []
        if inject_time:
            extra.append("现在时间：" + self._now())
        facts = self.recent_facts(max_facts)
        if facts:
            lines = []
            for cat, content, seen, cnt in facts:
                if cnt >= 2 or cat in ("preference", "aversion", "identity", "location", "occupation"):
                    lines.append(f"- {content}（{cat}）")
            if lines:
                extra.append("关于主人的长期记忆：" + "；".join(lines[:8]))
        recent = self.recent_conversation(max_recent)
        if recent:
            conv_txt = "；".join(
                ("我:" if r == "assistant" else "主人:") + c[:20]
                for r, c in recent)
            extra.append("最近的简短对话记忆：" + conv_txt)
        if extra:
            parts.append("【额外背景】" + "\n".join(extra))
        return "\n\n".join(parts)

    def stats(self):
        return {
            "facts": self.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
            "conv": self.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
        }


if __name__ == "__main__":
    # 自检示例
    db = MemoryDB(":memory:")
    db.save_facts([("preference", "喝下午茶")])
    print(db.build_prompt("测试人设", inject_time=True))
    print(db.stats())
