"""Smoke test: verify card_flags toggle logic and list_saved_cards against a
dummy source/card inserted directly, independent of any real scrape. Run with:
    python -m scripts.test_flags
"""
import json
import time

from dotenv import load_dotenv

from contentpilot.db import get_connection
from contentpilot.storage import VALID_FLAGS, list_saved_cards, mark_source_saved, toggle_card_flag

DUMMY_CANONICAL_URL = "https://example.com/__test_flags_dummy__"


def _insert_dummy_source_and_card() -> tuple[int, int]:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM sources WHERE canonical_url = ?", (DUMMY_CANONICAL_URL,))
        conn.commit()

        now = int(time.time())
        cursor = conn.execute(
            """
            INSERT INTO sources (
                source_type, external_id, canonical_url, title, published_at,
                fetched_at, raw_text, fetch_status
            ) VALUES ('article', 'dummy', ?, 'Test Flags Dummy', ?, ?, 'dummy text', 'ok')
            """,
            (DUMMY_CANONICAL_URL, now, now),
        )
        source_id = cursor.lastrowid

        extraction = {
            "title": "Test Flags Dummy",
            "summary": "A dummy card for flag toggle testing.",
            "content_type": "other",
            "topics": [],
            "key_learnings": ["dummy learning"],
            "build_ideas": [],
            "job_signals": [],
            "entities": {"people": [], "tools": [], "companies": []},
        }
        cursor = conn.execute(
            """
            INSERT INTO cards (
                source_id, lens, prompt_version, schema_version, model,
                extraction_json, summary, status, extracted_at
            ) VALUES (?, 'tpm', 'tpm_lens_v1', 1, 'dummy-model', ?, ?, 'ok', ?)
            """,
            (source_id, json.dumps(extraction), extraction["summary"], now),
        )
        card_id = cursor.lastrowid
        conn.commit()
        return source_id, card_id
    finally:
        conn.close()


def main() -> None:
    load_dotenv()

    source_id, card_id = _insert_dummy_source_and_card()
    print(f"1. Inserted dummy source_id={source_id} card_id={card_id}")

    mark_source_saved(source_id)

    is_set = toggle_card_flag(card_id, "study")
    assert is_set is True, f"Expected first toggle to set the flag, got {is_set}"
    print("2. toggle_card_flag(card_id, 'study') -> True (set)   PASS")

    is_set = toggle_card_flag(card_id, "study")
    assert is_set is False, f"Expected second toggle to clear the flag, got {is_set}"
    print("3. toggle_card_flag(card_id, 'study') again -> False (cleared)   PASS")

    for flag in VALID_FLAGS:
        result = toggle_card_flag(card_id, flag)
        assert result is True, f"Expected {flag} to be set, got {result}"
    print(f"4. All four flags set: {VALID_FLAGS}   PASS")

    conn = get_connection()
    try:
        stored = {
            row["flag"] for row in conn.execute(
                "SELECT flag FROM card_flags WHERE card_id = ?", (card_id,)
            ).fetchall()
        }
    finally:
        conn.close()
    assert stored == set(VALID_FLAGS), f"Expected {set(VALID_FLAGS)}, got {stored}"
    print(f"   Verified in card_flags table: {stored}   PASS")

    saved = list_saved_cards()
    dummy = next((c for c in saved if c["id"] == card_id), None)
    assert dummy is not None, "Dummy card not found in list_saved_cards() output"
    assert dummy["flags"] == set(VALID_FLAGS), f"Expected all flags set, got {dummy['flags']}"
    print(f"5. list_saved_cards() includes dummy card with flags={dummy['flags']}   PASS")

    # Cleanup
    conn = get_connection()
    try:
        conn.execute("DELETE FROM card_flags WHERE card_id = ?", (card_id,))
        conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        conn.commit()
    finally:
        conn.close()
    print("\nCleaned up dummy source/card.")
    print("PASS — all flag toggle checks passed.")


if __name__ == "__main__":
    main()
