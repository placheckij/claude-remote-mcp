"""Fakes for herdr shared by the bridge tests."""

import json
from collections.abc import Sequence


def ok(result: dict) -> tuple[int, str, str]:
    return 0, json.dumps({"id": "cli", "result": result}), ""


def err(code: str) -> tuple[int, str, str]:
    # Like the real CLI: exit 1 with the error envelope on stderr.
    return 1, "", json.dumps({"id": "cli", "error": {"code": code, "message": code}})


class FakeRunner:
    """Answers herdr invocations by matching their leading arguments."""

    def __init__(self, responses: dict[tuple[str, ...], tuple[int, str, str] | list]):
        self.responses = responses
        self.calls: list[list[str]] = []

    async def __call__(self, argv: Sequence[str]) -> tuple[int, str, str]:
        args = list(argv[1:])
        self.calls.append(args)
        for prefix in sorted(self.responses, key=len, reverse=True):
            if tuple(args[: len(prefix)]) == prefix:
                response = self.responses[prefix]
                # A list is consumed in order, its last entry repeating.
                if isinstance(response, list):
                    return response.pop(0) if len(response) > 1 else response[0]
                return response
        raise AssertionError(f"unexpected herdr call: {args}")

    def called(self, *prefix: str) -> list[list[str]]:
        return [call for call in self.calls if tuple(call[: len(prefix)]) == prefix]
