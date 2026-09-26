"""agent_loop.py on the scripted FakeProvider, for bench.py tests: fake_loop.py <script.json> <agent_loop args>."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(HERE), HERE]

import agent_loop  # noqa: E402
from fake_provider import FakeProvider  # noqa: E402

if __name__ == "__main__":
    script = sys.argv[1]
    sys.exit(agent_loop.main(sys.argv[2:], provider_factory=lambda a, schema: FakeProvider(script, model=a.model)))
