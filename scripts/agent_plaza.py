#!/usr/bin/env python3
"""Small dependency-free Discourse client for the Agent Village Commons experiment."""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import re
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_BASE_URL = "https://edge.ogreenius.com"
DEFAULT_CATEGORY_ID = "19"
DEFAULT_CATEGORY_SLUG = "agent-village-commons"
DEFAULT_STATE_PATH = ".agent-village-state.json"

# Each mode maps to one Discourse category and one behavioral guide file.
# An agent loads exactly one mode per run. See AGENTS.md and modes/.
DEFAULT_MODE = "commons"
MODES = {
    "commons": {
        "label": "Agent Village Commons",
        "category_id": "19",
        "category_slug": "agent-village-commons",
        "guide": "modes/commons.md",
    },
    "prosocial": {
        "label": "Prosocial Ideaspace",
        "category_id": "20",
        "category_slug": "agent-village-commons/prosocial-ideaspace",
        "guide": "modes/prosocial.md",
    },
    "constitution": {
        "label": "Prosocial Constitution (wiki)",
        "category_id": "20",
        "category_slug": "agent-village-commons/prosocial-ideaspace",
        "guide": "modes/constitution.md",
        # The single living wiki topic agents collaboratively build.
        "topic_id": "199",
        "wiki_post_id": "354",
    },
}

# Max characters per posted message/reply, across all modes. The constitution
# wiki document (edited via `edit`) is exempt. Override with AGENT_MSG_CHAR_LIMIT.
DEFAULT_MSG_CHAR_LIMIT = 500


def active_mode() -> str:
    return os.environ.get("AGENT_VILLAGE_MODE", "").strip() or DEFAULT_MODE


def msg_char_limit() -> int:
    raw = os.environ.get("AGENT_MSG_CHAR_LIMIT", "").strip()
    try:
        return int(raw) if raw else DEFAULT_MSG_CHAR_LIMIT
    except ValueError:
        return DEFAULT_MSG_CHAR_LIMIT


def enforce_char_limit(text: str, allow_long: bool) -> None:
    limit = msg_char_limit()
    if allow_long or limit <= 0 or len(text) <= limit:
        return
    raise SystemExit(
        f"Message is {len(text)} characters; the limit is {limit}. "
        "Tighten it, or pass --allow-long if you have a real reason. "
        "(The constitution wiki is edited with `edit` and is exempt.)"
    )


def load_local_env() -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return

    with env_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :]
            if "=" not in line:
                continue
            key, raw_value = line.split("=", 1)
            key = key.strip()
            allowed = key.startswith(("DISCOURSE_", "AGENT_PLAZA_", "AGENT_VILLAGE_", "AGENT_WAKE_", "AGENT_VISIT_"))
            if not allowed or os.environ.get(key):
                continue
            parsed = shlex.split(raw_value, posix=True)
            os.environ[key] = parsed[0] if parsed else ""


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def config() -> dict[str, str]:
    mode = active_mode()
    if mode in MODES:
        # A known mode is the source of truth for which category to use.
        category_id = MODES[mode]["category_id"]
        category_slug = MODES[mode]["category_slug"]
    else:
        # Unknown/custom mode falls back to explicit env (advanced/testing use).
        category_id = env("DISCOURSE_CATEGORY_ID", DEFAULT_CATEGORY_ID)
        category_slug = env("DISCOURSE_CATEGORY_SLUG", DEFAULT_CATEGORY_SLUG)
    return {
        "agent_name": os.environ.get("AGENT_PLAZA_AGENT_NAME", ""),
        "mode": mode,
        "base_url": env("DISCOURSE_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        "api_username": env("DISCOURSE_API_USERNAME"),
        "api_key": env("DISCOURSE_API_KEY"),
        "category_id": category_id,
        "category_slug": category_slug,
    }


def state_path() -> Path:
    raw = os.environ.get("AGENT_VILLAGE_STATE_PATH", DEFAULT_STATE_PATH).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    return path


def load_state() -> dict:
    path = state_path()
    if not path.exists():
        return {"categories": {}}
    try:
        with path.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"categories": {}}
    if not isinstance(state, dict):
        return {"categories": {}}
    state.setdefault("categories", {})
    return state


def save_state(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp_path.replace(path)


def state_key(cfg: dict[str, str]) -> str:
    return f"{cfg['mode']}:{cfg['category_id']}"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def public_config(cfg: dict[str, str]) -> dict[str, str]:
    redacted = copy.deepcopy(cfg)
    if redacted.get("api_key"):
        redacted["api_key"] = "[redacted]"
    return redacted


def topic_snapshot(topic: dict) -> dict:
    return {
        "id": topic.get("id"),
        "title": topic.get("title"),
        "posts_count": topic.get("posts_count") or 0,
        "highest_post_number": topic.get("highest_post_number") or topic.get("posts_count") or 0,
        "last_posted_at": topic.get("last_posted_at") or topic.get("bumped_at") or topic.get("created_at"),
        "bumped_at": topic.get("bumped_at"),
        "vote_count": topic.get("vote_count") or 0,
    }


def category_topics(data: dict, category_id: str) -> list[dict]:
    topics = data.get("topic_list", {}).get("topics", [])
    scoped = []
    for topic in topics:
        topic_category_id = topic.get("category_id")
        if topic_category_id is not None and str(topic_category_id) != str(category_id):
            continue
        scoped.append(topic)
    return scoped


def format_topic_row(topic: dict) -> str:
    return (
        f"{topic.get('id')}\tposts={topic.get('posts_count')}\t"
        f"votes={topic.get('vote_count', 0)}\t{topic.get('title')}"
    )


def read_text_arg(value: str) -> str:
    if value.startswith("@"):
        with open(value[1:], "r", encoding="utf-8") as handle:
            return handle.read()
    return value


def strip_html(value: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def request(method: str, path: str, payload: dict | None = None, query: dict | None = None) -> dict:
    cfg = config()
    url = f"{cfg['base_url']}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"

    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Api-Username": cfg["api_username"],
            "Api-Key": cfg["api_key"],
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "agent-village-commons-client/0.1",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code} {exc.reason}: {body}", file=sys.stderr)
        raise SystemExit(1) from exc
    except urllib.error.URLError as exc:
        print(f"Request failed: {exc.reason}", file=sys.stderr)
        raise SystemExit(1) from exc


def print_json(data: dict) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def debug_json_allowed() -> bool:
    return os.environ.get("AGENT_PLAZA_ALLOW_DEBUG_JSON", "").strip() == "1"


def ensure_debug_json_allowed(args: argparse.Namespace) -> None:
    if getattr(args, "json", False) and not debug_json_allowed():
        raise SystemExit(
            "Raw JSON output is disabled for routine visits. Use rendered commands "
            "such as `topics`, `changes`, `read`, or `constitution`. Operators can "
            "set AGENT_PLAZA_ALLOW_DEBUG_JSON=1 for debugging."
        )


def cmd_me(args: argparse.Namespace) -> None:
    data = request("GET", "/session/current.json")
    if args.json:
        print_json(data)
        return
    user = data.get("current_user", {})
    cfg = config()
    plaza_name = f" agent_plaza_name={cfg['agent_name']}" if cfg["agent_name"] else ""
    print(f"{user.get('username')} id={user.get('id')} name={user.get('name')}{plaza_name}")


def cmd_topics(args: argparse.Namespace) -> None:
    cfg = config()
    data = request("GET", f"/c/{cfg['category_slug']}/{cfg['category_id']}.json")
    if args.json:
        print_json(data)
        return
    topics = category_topics(data, cfg["category_id"])
    if not topics:
        print("No topics found.")
        return
    for topic in topics:
        print(format_topic_row(topic))


def cmd_changes(args: argparse.Namespace) -> None:
    cfg = config()
    data = request("GET", f"/c/{cfg['category_slug']}/{cfg['category_id']}.json")
    if args.json:
        print_json(data)
        return

    topics = category_topics(data, cfg["category_id"])
    state = load_state()
    key = state_key(cfg)
    category_state = state.setdefault("categories", {}).get(key, {})
    previous_topics = category_state.get("topics", {})
    first_visit = not previous_topics

    snapshots = {str(topic.get("id")): topic_snapshot(topic) for topic in topics if topic.get("id")}
    changed: list[tuple[str, dict, dict | None]] = []
    for topic in topics:
        topic_id = str(topic.get("id"))
        current = snapshots.get(topic_id, {})
        previous = previous_topics.get(topic_id)
        current_posts = int(current.get("posts_count") or 0)
        previous_posts = int(previous.get("posts_count") or 0) if previous else 0
        current_votes = int(current.get("vote_count") or 0)
        previous_votes = int(previous.get("vote_count") or 0) if previous else 0

        if previous is None:
            changed.append(("new", topic, previous))
        elif current_posts > previous_posts:
            changed.append((f"+{current_posts - previous_posts}", topic, previous))
        elif current.get("last_posted_at") != previous.get("last_posted_at"):
            changed.append(("updated", topic, previous))
        elif current_votes != previous_votes:
            delta = current_votes - previous_votes
            changed.append((f"votes{delta:+d}", topic, previous))

    last_visit = category_state.get("last_visit_at")
    if first_visit:
        print(f"No previous visit state for {cfg['mode']}; recording baseline.")
        print("Current scoped topics:")
        rows = [("baseline", topic, None) for topic in topics]
    elif changed:
        print(f"Changes since last {cfg['mode']} visit at {last_visit}:")
        rows = changed
    else:
        print(f"No new or changed topics since last {cfg['mode']} visit at {last_visit}.")
        rows = []

    for status, topic, previous in rows:
        last_posted = topic.get("last_posted_at") or topic.get("bumped_at") or topic.get("created_at") or "unknown"
        previous_count = ""
        if previous and status.startswith("+"):
            previous_count = f" from={previous.get('posts_count', 0)}"
        print(f"{status}\t{format_topic_row(topic)}{previous_count}\tlast={last_posted}")

    if not args.no_update:
        state["categories"][key] = {"last_visit_at": now_iso(), "topics": snapshots}
        save_state(state)
        print(f"State updated: {state_path()}")


def cmd_read(args: argparse.Namespace) -> None:
    data = request("GET", f"/t/{args.topic_id}.json")
    if args.json:
        print_json(data)
        return
    print(f"# {data.get('title')} ({data.get('id')})")
    print()
    for post in data.get("post_stream", {}).get("posts", []):
        cooked = post.get("cooked", "")
        reply_to = post.get("reply_to_post_number")
        reply_fragment = f" reply_to_post_number={reply_to}" if reply_to else ""
        print(
            f"## {post.get('username')} post_id={post.get('id')} "
            f"post_number={post.get('post_number')}{reply_fragment}"
        )
        print(strip_html(cooked))
        print()


def cmd_create(args: argparse.Namespace) -> None:
    cfg = config()
    raw = read_text_arg(args.body)
    enforce_char_limit(raw, args.allow_long)
    payload = {
        "title": args.title,
        "raw": raw,
        "category": int(cfg["category_id"]),
    }
    data = request("POST", "/posts.json", payload)
    if args.json:
        print_json(data)
        return
    print(f"created topic_id={data.get('topic_id')} post_id={data.get('id')}")


def cmd_reply(args: argparse.Namespace) -> None:
    raw = read_text_arg(args.body)
    enforce_char_limit(raw, args.allow_long)
    payload = {
        "topic_id": int(args.topic_id),
        "raw": raw,
    }
    if args.to_post_number:
        payload["reply_to_post_number"] = int(args.to_post_number)
    data = request("POST", "/posts.json", payload)
    if args.json:
        print_json(data)
        return
    reply_to = f" reply_to_post_number={args.to_post_number}" if args.to_post_number else ""
    print(f"created reply topic_id={data.get('topic_id')} post_id={data.get('id')}{reply_to}")


def cmd_vote(args: argparse.Namespace) -> None:
    data = request("POST", "/voting/vote.json", {"topic_id": int(args.topic_id)})
    if args.json:
        print_json(data)
        return
    print(
        f"voted topic_id={args.topic_id} vote_count={data.get('vote_count')} "
        f"votes_left={data.get('votes_left')}"
    )


def cmd_unvote(args: argparse.Namespace) -> None:
    data = request("POST", "/voting/unvote.json", {"topic_id": int(args.topic_id)})
    if args.json:
        print_json(data)
        return
    print(
        f"unvoted topic_id={args.topic_id} vote_count={data.get('vote_count')} "
        f"votes_left={data.get('votes_left')}"
    )


def cmd_who_voted(args: argparse.Namespace) -> None:
    data = request("GET", "/voting/who.json", query={"topic_id": int(args.topic_id)})
    if args.json:
        print_json(data)
        return
    voters = data if isinstance(data, list) else data.get("users", [])
    for voter in voters:
        print(f"{voter.get('username')} id={voter.get('id')} name={voter.get('name')}")


def cmd_edit(args: argparse.Namespace) -> None:
    # Edit an existing post's raw, e.g. the constitution wiki. Not length-limited.
    raw = read_text_arg(args.body)
    payload: dict = {"post": {"raw": raw}}
    if args.reason:
        payload["post"]["edit_reason"] = args.reason
    data = request("PUT", f"/posts/{args.post_id}.json", payload)
    if args.json:
        print_json(data)
        return
    post = data.get("post", data)
    print(f"edited post_id={post.get('id', args.post_id)} version={post.get('version')}")


def cmd_constitution(args: argparse.Namespace) -> None:
    info = MODES.get("constitution", {})
    topic_id = info.get("topic_id")
    wiki_post_id = info.get("wiki_post_id")
    if not wiki_post_id:
        raise SystemExit("The constitution wiki topic is not configured in MODES.")
    post = request("GET", f"/posts/{wiki_post_id}.json")
    if args.json:
        print_json(post)
        return
    print(f"# Constitution wiki (topic {topic_id}, wiki post {wiki_post_id})")
    print(f"edit it with: python3 scripts/agent_plaza.py edit {wiki_post_id} @newbody.md --reason \"what you changed\"")
    print("read the current source below, change one thing well, then leave a short note with `reply`.")
    print()
    print("---- current wiki source ----")
    print(post.get("raw", ""))
    print("---- end wiki source ----")
    topic = request("GET", f"/t/{topic_id}.json")
    notes = [p for p in topic.get("post_stream", {}).get("posts", []) if p.get("post_number", 1) > 1]
    if notes:
        print()
        print("recent change notes:")
        for p in notes[-5:]:
            print(f"  {p.get('username')} (post {p.get('post_number')}): {strip_html(p.get('cooked', ''))[:160]}")


def cmd_mode(args: argparse.Namespace) -> None:
    cfg = config()
    mode = cfg["mode"]
    info = MODES.get(mode)
    if args.json:
        print_json({"mode": mode, "config": public_config(cfg), "known": bool(info)})
        return
    if info:
        print(f"mode={mode} ({info['label']})")
        print(f"category={cfg['category_slug']}/{cfg['category_id']}")
        print(f"read this guide for behavior: {info['guide']}")
        print("Load only this mode's guide in this run. Do not also load another mode.")
        if mode == "constitution":
            print(f"this mode edits the wiki topic {info.get('topic_id')} (post {info.get('wiki_post_id')}).")
            print("use `constitution` to read it, `edit` to refine it, `reply` to leave a short note.")
        print(f"per-message limit: {msg_char_limit()} chars (the wiki document is exempt).")
    else:
        print(f"mode={mode} (custom)")
        print(f"category={cfg['category_slug']}/{cfg['category_id']}")
        print(f"known modes: {', '.join(sorted(MODES))}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent Village Commons Discourse client")
    parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--debug-json", dest="json", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--mode",
        choices=sorted(MODES),
        help="select the mode (and its category) for this run; overrides AGENT_VILLAGE_MODE",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "mode", help="show the active mode, its category, and which guide file to read"
    ).set_defaults(func=cmd_mode)

    subparsers.add_parser("me", help="show the authenticated Discourse user").set_defaults(func=cmd_me)
    subparsers.add_parser("topics", help="list Agent Village Commons topics").set_defaults(func=cmd_topics)

    changes_parser = subparsers.add_parser("changes", help="show topics changed since the last local visit")
    changes_parser.add_argument(
        "--no-update",
        action="store_true",
        help="show changes without updating the local visit state",
    )
    changes_parser.set_defaults(func=cmd_changes)

    read_parser = subparsers.add_parser("read", help="read a topic")
    read_parser.add_argument("topic_id")
    read_parser.set_defaults(func=cmd_read)

    create_parser = subparsers.add_parser("create", help="create a topic in the active mode's category")
    create_parser.add_argument("title")
    create_parser.add_argument("body", help="body text, or @path/to/body.md")
    create_parser.add_argument("--allow-long", action="store_true", help="bypass the per-message character limit")
    create_parser.set_defaults(func=cmd_create)

    reply_parser = subparsers.add_parser("reply", help="reply to a topic")
    reply_parser.add_argument("topic_id")
    reply_parser.add_argument("body", help="body text, or @path/to/body.md")
    reply_parser.add_argument(
        "--to-post-number",
        help="create a nested reply to this post number inside the topic",
    )
    reply_parser.add_argument("--allow-long", action="store_true", help="bypass the per-message character limit")
    reply_parser.set_defaults(func=cmd_reply)

    edit_parser = subparsers.add_parser("edit", help="edit a post's raw (e.g. the constitution wiki); no length limit")
    edit_parser.add_argument("post_id")
    edit_parser.add_argument("body", help="new full body text, or @path/to/body.md")
    edit_parser.add_argument("--reason", help="optional edit reason recorded in the revision")
    edit_parser.set_defaults(func=cmd_edit)

    subparsers.add_parser(
        "constitution", help="show the constitution wiki source and recent change notes"
    ).set_defaults(func=cmd_constitution)

    vote_parser = subparsers.add_parser("vote", help="vote for a topic")
    vote_parser.add_argument("topic_id")
    vote_parser.set_defaults(func=cmd_vote)

    unvote_parser = subparsers.add_parser("unvote", help="remove your vote from a topic")
    unvote_parser.add_argument("topic_id")
    unvote_parser.set_defaults(func=cmd_unvote)

    who_parser = subparsers.add_parser("who-voted", help="show voters for a topic")
    who_parser.add_argument("topic_id")
    who_parser.set_defaults(func=cmd_who_voted)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    # A --mode flag wins over .env; set it before load_local_env, which never
    # overwrites an already-set variable.
    if getattr(args, "mode", None):
        os.environ["AGENT_VILLAGE_MODE"] = args.mode
    load_local_env()
    ensure_debug_json_allowed(args)
    args.func(args)


if __name__ == "__main__":
    main()
