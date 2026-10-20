import unittest

from beework.runtime.demo_graph import demo_graph


class DemoGraphTests(unittest.TestCase):
    def test_graph_runs_and_returns_state(self) -> None:
        result = demo_graph.invoke({"message": "hello"})

        self.assertEqual(result["message"], "LangGraph is ready: hello")


if __name__ == "__main__":
    unittest.main()
