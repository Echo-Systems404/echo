"""Manual check against a live model; not part of the unit tests.
End-to-end integration test for the agent harness."""

from pathlib import Path
import sys

import config
import tools
import memory
import engine
import seeds

TARGET_FILE = Path("test_ok.txt")
EXPECTED_CONTENT = "HELLO_WORLD"
QUERY = "Create a file named test_ok.txt containing the exact word HELLO_WORLD"


def main() -> int:
    pre_existing = TARGET_FILE.exists()

    mem = memory.ConversationMemory(
        system_prompt=memory.build_system_prompt(
            config.SYSTEM_PROMPT, config.SELF_FILE, config.USER_FILE, seeds.render_pilot(tools.TOOLS)
        )
    )
    agent = engine.AgentEngine(config=config, tools=tools.TOOLS, memory=mem)

    try:
        response = agent.run(QUERY)
        assert response is not None, "agent.run() returned None"

        assert TARGET_FILE.exists(), f"Expected {TARGET_FILE} to exist after agent run"
        actual = TARGET_FILE.read_text(encoding="utf-8")
        assert EXPECTED_CONTENT in actual, (
            f"Expected {EXPECTED_CONTENT!r} in {TARGET_FILE}, got {actual!r}"
        )
    finally:
        # Only remove the file if this test created it; never delete a pre-existing file.
        if not pre_existing and TARGET_FILE.exists():
            TARGET_FILE.unlink()

    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
