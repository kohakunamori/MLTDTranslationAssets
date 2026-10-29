"""Source-only scene context for the production Assets queue (no model/API I/O)."""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import re

from pipelines.core.hashing import sha256_text
from pipelines.core.jsonl import read_jsonl

SPEECH_KEY = re.compile(r"^(?P<scene>.+)_(?P<order>\d{4})_(?P<speaker>\d{3}[a-z]{3}|p)$", re.I)
COMMAND_KEY = re.compile(r"^commands\[(\d+)\]\.text$")
MAIL_KEY = re.compile(r"^mail_(?P<speaker>\d{3}[a-z]{3})_\d+_text$", re.I)
MAX_CONTEXTS = 3


def speaker_evidence(path: Path) -> dict:
    """Use the checked-in identity registry, never historical translated samples."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    speakers = {
        str(row["speaker_code"]): {"name_jp": row["name_jp"]}
        for row in doc["idols"] if row.get("speaker_code") and row.get("name_jp")
    }
    speakers["p"] = {"name_jp": "プロデューサー"}
    return {"schema_version": 1, "speakers": speakers}


def enrich_queue(root: Path, queue: list[dict], paths: list[tuple[Path, str]], *, window_size: int = 3) -> None:
    """Enrich in place while retaining the existing source-hash output identity.

    Only numbered speech keys within one scene and ordered lyric slots establish
    neighbors. Master delta files are partial inventories, not scene transcripts.
    Shared source text accumulates all speakers before any task is selected.
    Read one file at a time; never load the entire locale catalogue into memory.
    """
    if window_size not in range(1, 5):
        raise ValueError("context window must be between 1 and 4")
    wanted = {row["source_sha256"]: row for row in queue}
    if not wanted:
        return
    states: dict[str, dict] = {}
    for path, kind in paths:
        file_rows = read_jsonl(path)
        category = "LIVE_LYRICS" if kind == "lyrics" else path.relative_to(root / "locales").parts[0]
        groups: dict[tuple, list[tuple[int, dict]]] = defaultdict(list)
        for row in file_rows:
            key = str(row.get("item_key", ""))
            speech = SPEECH_KEY.fullmatch(key)
            mail = MAIL_KEY.fullmatch(key)
            command = COMMAND_KEY.fullmatch(key) if category == "event-unit" else None
            sid = str(row.get("source_sha256", ""))
            if sid in wanted:
                if sid != sha256_text(str(row.get("ja", ""))):
                    raise ValueError(f"invalid context source identity: {path}")
                state = states.setdefault(sid, {"speakers": set(), "categories": set(), "surfaces": set(), "occurrences": 0,
                                                "contexts": [], "seen": set()})
                state["occurrences"] += 1
                state["categories"].add(category)
                if speech:
                    state["speakers"].add(speech["speaker"].lower())
                if mail:
                    state["speakers"].add(mail["speaker"].lower())
                state["surfaces"].add("MAIL" if mail else kind)
            if category == "master":
                continue
            group = (row.get("bundle"), row.get("asset_version"))
            if kind == "lyrics" and isinstance(row.get("index"), int):
                groups[group].append((row["index"], row))
            elif speech:
                groups[(*group, speech["scene"])].append((int(speech["order"]), row))
            elif command:
                groups[group].append((int(command[1]), row))
        for group_rows in groups.values():
            ordered = sorted(group_rows, key=lambda pair: pair[0])
            for index, (order, row) in enumerate(ordered):
                sid = str(row.get("source_sha256", ""))
                if sid not in wanted:
                    continue
                state = states[sid]
                if len(state["contexts"]) >= MAX_CONTEXTS:
                    continue
                window = []
                for peer_index in range(max(0, index - window_size), min(len(ordered), index + window_size + 1)):
                    peer_order, peer = ordered[peer_index]
                    if kind != "lyrics" and abs(peer_order - order) != abs(peer_index - index):
                        # A gap can mark a branch or a partial source inventory.
                        # Missing context is better evidence than a false neighbor.
                        continue
                    source = str(peer.get("ja", ""))
                    if peer.get("source_sha256") != sha256_text(source):
                        raise ValueError(f"invalid neighboring source identity: {path}")
                    peer_context = {"relative": peer_index - index, "source": source}
                    peer_speech = SPEECH_KEY.fullmatch(str(peer.get("item_key", "")))
                    if peer_speech:
                        peer_context["speaker_code"] = peer_speech["speaker"].lower()
                    window.append(peer_context)
                signature = (row.get("bundle"), tuple((p["relative"], p["source"], p.get("speaker_code")) for p in window))
                if signature not in state["seen"]:
                    state["seen"].add(signature)
                    state["contexts"].append({"logical": row.get("bundle", ""), "context": window})
    for sid, queued in wanted.items():
        state = states[sid]
        speakers = sorted(state["speakers"])
        categories = sorted(state["categories"])
        queued["examples"] = [{"key": queued["key"]}]
        queued["context_examples"] = state["contexts"]
        queued["usage_profile"] = {
            "categories": categories, "speaker_codes": speakers,
            "catalogue_occurrences": state["occurrences"],
            "multi_speaker": len(speakers) > 1, "multi_category": len(categories) > 1,
        }
        if queued["task"] == "ASSETS_LYRICS":
            queued["usage_profile"]["category"] = "LIVE_LYRICS"
        elif state["surfaces"] == {"MAIL"}:
            queued["surface_hint"] = "MAIL"
        elif categories == ["event-unit"]:
            # These source rows carry command text but no speaker metadata.
            # Never infer a speaker from a unit's membership or its filename.
            queued["task_hint"] = "DIALOGUE"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--idols", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(speaker_evidence(args.idols), ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
