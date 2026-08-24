"""Configuration safety tests for the local PR autopilot."""

from __future__ import annotations

from contextlib import redirect_stderr
import ctypes
from dataclasses import replace
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, call, patch
from urllib.error import HTTPError

import pr_autopilot
from pr_autopilot import (
    CommandError,
    Config,
    Controller,
    GitHubClient,
    KanbanClient,
    PRSummary,
    _NO_REDIRECT_OPENER,
    _NoRedirectHandler,
    main,
    repository_storage_slug,
)
from pr_reconciler import (
    Classification,
    ClassificationResult,
    MergeHistoryRecord,
    PRState,
    StateStore,
    classify_pr,
    findings_fingerprint,
)


HEAD = "a" * 40


def exact_merged_status(head_sha: str = HEAD) -> dict[str, str]:
    return {
        "state": "MERGED",
        "headRefOid": head_sha,
        "mergedAt": "2026-08-10T20:00:01Z",
    }


def valid_config() -> dict:
    return {
        "analyzer_profile": "prtriage",
        "analyzer_skill": "pr-autopilot-analyzer",
        "analysis_task_timeout": "90m",
        "analysis_max_turns": 18,
        "worker_profile": "prfix",
        "worker_skill": "pr-autopilot-fixer",
        "fix_max_turns": 40,
        "verifier_profile": "prverify",
        "verifier_skill": "pr-autopilot-verifier",
        "verification_task_timeout": "60m",
        "verification_max_turns": 20,
        "policy_revision": 1,
        "pause_labels": ["hermes-pause"],
        "excluded_repositories": [],
        "disabled_repositories": [],
        "recovery_endpoint_hosts": [
            "spark.example.test",
            "down.example.test",
            "old.example.test",
        ],
        "recovery_model_id": "dgx-model",
        "recovery_api_key_env": "TEST_RECOVERY_API_KEY",
        "recovery_api_key_file": "missing-recovery-key",
        "max_open_prs": 100,
        "max_review_rounds": 5,
        "task_timeout": "180m",
        "fix_progress_extension": "60m",
        "fix_runtime_cap": "48h",
        "task_max_retries": 0,
    }


class RecordingRunner:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.json_calls: list[list[str]] = []
        self.run_calls: list[list[str]] = []

    def run_json(self, arguments: list[str], **_kwargs: object) -> object:
        self.json_calls.append(arguments)
        return self.responses.pop(0)

    def run(self, arguments: list[str], **_kwargs: object) -> str:
        self.run_calls.append(arguments)
        return ""


class WorkspaceRecordingRunner(RecordingRunner):
    def run(self, arguments: list[str], **_kwargs: object) -> str:
        self.run_calls.append(arguments)
        if arguments[-3:] == ["remote", "get-url", "origin"]:
            return "https://github.com/AnikaWilliams/example.git\n"
        if arguments[-2:] == ["rev-parse", "HEAD"]:
            return f"{HEAD}\n"
        return ""


class ProgressPushRunner(RecordingRunner):
    def __init__(self, local_head: str) -> None:
        super().__init__([])
        self.local_head = local_head

    def run(self, arguments: list[str], **_kwargs: object) -> str:
        self.run_calls.append(arguments)
        if arguments[-2:] == ["rev-parse", "HEAD"]:
            return self.local_head + "\n"
        if "merge-base" in arguments and "--is-ancestor" in arguments:
            return ""
        return ""


class FailingCompletionRunner(RecordingRunner):
    def run(self, arguments: list[str], **_kwargs: object) -> str:
        self.run_calls.append(arguments)
        if arguments[:3] == ["hermes", "kanban", "complete"]:
            raise CommandError("simulated Kanban completion failure")
        return ""


class FirstCommentFailsRunner(RecordingRunner):
    def __init__(self, responses: list[object]) -> None:
        super().__init__(responses)
        self.failed = False

    def run(self, arguments: list[str], **_kwargs: object) -> str:
        self.run_calls.append(arguments)
        if arguments[:3] == ["hermes", "kanban", "comment"] and not self.failed:
            self.failed = True
            raise CommandError("simulated Kanban comment failure")
        return ""


class NetworkLogRunner(RecordingRunner):
    def __init__(self, responses: list[object], task_log: str) -> None:
        super().__init__(responses)
        self.task_log = task_log

    def run(self, arguments: list[str], **_kwargs: object) -> str:
        self.run_calls.append(arguments)
        if arguments[:3] == ["hermes", "kanban", "log"]:
            return self.task_log
        return ""


class ConfigTests(unittest.TestCase):
    def test_transient_offline_tick_is_silent_and_returns_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            messages = (
                "error connecting to api.github.com; check your internet connection",
                "proxyconnect tcp: connectex: target machine actively refused it",
            )
            for message in messages:
                with self.subTest(message=message):
                    stderr = StringIO()
                    with patch("pr_autopilot.Controller") as controller_type:
                        controller_type.return_value.run.side_effect = CommandError(message)
                        with redirect_stderr(stderr):
                            result = main(["--config", str(path), "--dry-run"])

                    self.assertEqual(result, 0)
                    self.assertEqual(stderr.getvalue(), "")

    def test_legacy_cli_rejects_live_mutating_cycles_before_lock_or_controller(self) -> None:
        """The legacy entry point must not bypass Desktop lifecycle controls."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            stderr = StringIO()

            with (
                patch("pr_autopilot._acquire_controller_lock") as acquire_lock,
                patch("pr_autopilot.Controller") as controller_type,
                redirect_stderr(stderr),
            ):
                result = main(["--config", str(path)])

            self.assertEqual(result, 2)
            self.assertIn("live runs are disabled", stderr.getvalue())
            acquire_lock.assert_not_called()
            controller_type.assert_not_called()

    def test_rejects_the_deprecated_pr_opt_in_label(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(
                json.dumps({**valid_config(), "opt_in_label": "hermes-automerge"}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "opt_in_label"):
                Config.load(path)

    def test_rejects_an_invalid_policy_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(
                json.dumps({**valid_config(), "policy_revision": 0}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "policy_revision"):
                Config.load(path)

    def test_rejects_the_deprecated_repository_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(
                json.dumps({**valid_config(), "allowed_repositories": []}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "excluded_repositories"):
                Config.load(path)

    def test_rejects_a_non_list_repository_opt_out_list(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(
                json.dumps(
                    {**valid_config(), "excluded_repositories": "AnikaWilliams/example"}
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                Config.load(path)

    def test_requires_zero_automatic_task_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(
                json.dumps({**valid_config(), "task_max_retries": 0}),
                encoding="utf-8",
            )

            self.assertEqual(Config.load(path).task_max_retries, 0)

            path.write_text(
                json.dumps({**valid_config(), "task_max_retries": 1}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "must be 0"):
                Config.load(path)


class GitHubClientTests(unittest.TestCase):
    def test_authored_open_prs_are_sorted_by_recent_update(self) -> None:
        runner = RecordingRunner([[]])

        self.assertEqual(GitHubClient(runner).authored_open_prs(100), [])

        self.assertEqual(
            runner.json_calls[0][3:13],
            [
                "--author",
                "@me",
                "--state",
                "open",
                "--sort",
                "updated",
                "--order",
                "desc",
                "--limit",
                "100",
            ],
        )

    def test_authored_repositories_are_distinct_and_sorted(self) -> None:
        class FakeRunner:
            def __init__(self) -> None:
                self.calls: list[list[str]] = []

            def run_json(self, arguments: list[str], **_kwargs: object) -> object:
                self.calls.append(arguments)
                return [
                    {"repository": {"nameWithOwner": "AnikaWilliams/example"}},
                    {"repository": {"nameWithOwner": "AnikaWilliams/example"}},
                    {"repository": {"nameWithOwner": "AnikaWilliams/other"}},
                    {"repository": {"nameWithOwner": "Zebra/zoo"}},
                ]

        runner = FakeRunner()
        client = GitHubClient(runner)  # type: ignore[arg-type]

        self.assertEqual(
            client.authored_repositories(),
            ["AnikaWilliams/example", "AnikaWilliams/other", "Zebra/zoo"],
        )
        self.assertEqual(runner.calls[0][:6], ["gh", "search", "prs", "--author", "@me", "--limit"])

    def test_detail_paginates_issue_comments_before_returning_them(self) -> None:
        request = {
            "id": "request-1",
            "createdAt": "2026-08-22T00:00:00Z",
            "body": "@codex review",
        }
        response = {
            "id": "response-1",
            "createdAt": "2026-08-22T00:01:00Z",
            "body": "Codex completed the review.",
        }
        runner = RecordingRunner(
            [
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "labels": {
                                    "nodes": [],
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                },
                                "comments": {
                                    "nodes": [request],
                                    "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                                }
                            }
                        }
                    }
                },
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "comments": {
                                    "nodes": [response],
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                }
                            }
                        }
                    }
                },
            ]
        )

        detail = GitHubClient(runner).detail("AnikaWilliams/example", 42)

        self.assertEqual(
            [comment["id"] for comment in detail["comments"]["nodes"]],
            ["request-1", "response-1"],
        )
        self.assertIn("after=cursor-1", runner.json_calls[1])

    def test_detail_paginates_labels_before_eligibility_checks(self) -> None:
        labels = [{"name": f"label-{index}"} for index in range(100)]
        runner = RecordingRunner(
            [
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "comments": {
                                    "nodes": [],
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                },
                                "labels": {
                                    "nodes": labels,
                                    "pageInfo": {"hasNextPage": True, "endCursor": "labels-100"},
                                },
                            }
                        }
                    }
                },
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "labels": {
                                    "nodes": [{"name": "hermes-pause"}],
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                }
                            }
                        }
                    }
                },
            ]
        )
        detail = GitHubClient(runner).detail("AnikaWilliams/example", 42)
        summary = PRSummary(
            repository="AnikaWilliams/example",
            number=42,
            title="Example",
            url="https://github.com/AnikaWilliams/example/pull/42",
            updated_at="2026-08-22T00:00:00Z",
            is_draft=False,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=runner)  # type: ignore[arg-type]

            self.assertFalse(controller._eligible(summary, detail))

        self.assertEqual(detail["labels"]["nodes"][-1], {"name": "hermes-pause"})
        self.assertIn("after=labels-100", runner.json_calls[1])

    def test_detail_paginates_older_reviews_for_current_head_codex_findings(self) -> None:
        finding = {"databaseId": 701, "body": "![P1 Badge] Fix the exact-head bug."}
        runner = RecordingRunner(
            [
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "headRefOid": HEAD,
                                "labels": {
                                    "nodes": [],
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                },
                                "comments": {
                                    "nodes": [],
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                },
                                "reviews": {
                                    "nodes": [],
                                    "pageInfo": {
                                        "hasPreviousPage": True,
                                        "startCursor": "reviews-100",
                                    },
                                },
                            }
                        }
                    }
                },
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviews": {
                                    "nodes": [
                                        {
                                            "id": "review-100",
                                            "author": {"login": "chatgpt-codex-connector"},
                                            "body": f"Reviewed commit: `{HEAD}`",
                                            "commit": {"oid": HEAD},
                                            "state": "COMMENTED",
                                            "comments": {
                                                "nodes": [],
                                                "pageInfo": {
                                                    "hasNextPage": True,
                                                    "endCursor": "inline-100",
                                                },
                                            },
                                        }
                                    ],
                                    "pageInfo": {
                                        "hasPreviousPage": False,
                                        "startCursor": None,
                                    },
                                }
                            }
                        }
                    }
                },
                {
                    "data": {
                        "node": {
                            "comments": {
                                "nodes": [finding],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                },
            ]
        )

        detail = GitHubClient(runner).detail("AnikaWilliams/example", 42)
        classification = classify_pr(detail, requested_head=HEAD)

        self.assertEqual(classification.kind, Classification.FINDINGS)
        self.assertEqual(classification.reviewed_sha, HEAD)
        self.assertEqual(classification.findings, (finding,))
        self.assertIn("before=reviews-100", runner.json_calls[1])
        self.assertIn("after=inline-100", runner.json_calls[2])

    def test_detail_paginates_inline_findings_for_an_initial_review_page(self) -> None:
        finding = {"databaseId": 702, "body": "Fix the exact initial-review finding."}
        runner = RecordingRunner(
            [
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "headRefOid": HEAD,
                                "labels": {
                                    "nodes": [],
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                },
                                "comments": {
                                    "nodes": [],
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                },
                                "reviews": {
                                    "nodes": [
                                        {
                                            "id": "initial-review-id",
                                            "author": {"login": "chatgpt-codex-connector"},
                                            "body": "### Codex Review",
                                            "commit": {"oid": HEAD},
                                            "state": "COMMENTED",
                                            "comments": {
                                                "nodes": [],
                                                "pageInfo": {
                                                    "hasNextPage": True,
                                                    "endCursor": "initial-inline-100",
                                                },
                                            },
                                        }
                                    ],
                                    "pageInfo": {
                                        "hasPreviousPage": False,
                                        "startCursor": None,
                                    },
                                },
                            }
                        }
                    }
                },
                {
                    "data": {
                        "node": {
                            "comments": {
                                "nodes": [finding],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                },
            ]
        )

        detail = GitHubClient(runner).detail("AnikaWilliams/example", 42)
        classification = classify_pr(detail, requested_head=HEAD)

        self.assertEqual(classification.kind, Classification.FINDINGS)
        self.assertEqual(classification.reviewed_sha, HEAD)
        self.assertEqual(classification.findings, (finding,))
        self.assertIn("inReplyTo { databaseId }", "\n".join(runner.json_calls[0]))
        self.assertIn("reviewId=initial-review-id", runner.json_calls[1])
        self.assertIn("after=initial-inline-100", runner.json_calls[1])
        self.assertIn("inReplyTo { databaseId }", "\n".join(runner.json_calls[1]))

    def test_detail_paginates_only_the_tracked_request_reactions(self) -> None:
        for content, expected_kind in (
            ("THUMBS_UP", Classification.CLEAN),
            ("EYES", Classification.REVIEWING),
        ):
            with self.subTest(content=content):
                request = {
                    "id": "request-node-id",
                    "databaseId": 101,
                    "body": "@codex review",
                    "createdAt": "2026-08-22T00:00:00Z",
                    "reactions": {
                        "nodes": [
                            {"content": "HEART", "user": {"login": f"person-{index}"}}
                            for index in range(100)
                        ],
                        "pageInfo": {
                            "hasPreviousPage": True,
                            "startCursor": "request-reactions-100",
                        },
                    },
                }
                other_comment = {
                    "id": "other-node-id",
                    "databaseId": 102,
                    "body": "untracked comment",
                    "createdAt": "2026-08-22T00:01:00Z",
                    "reactions": {
                        "nodes": [],
                        "pageInfo": {
                            "hasPreviousPage": True,
                            "startCursor": "must-not-fetch",
                        },
                    },
                }
                runner = RecordingRunner(
                    [
                        {
                            "data": {
                                "repository": {
                                    "pullRequest": {
                                        "headRefOid": HEAD,
                                        "labels": {
                                            "nodes": [],
                                            "pageInfo": {
                                                "hasNextPage": False,
                                                "endCursor": None,
                                            },
                                        },
                                        "comments": {
                                            "nodes": [request, other_comment],
                                            "pageInfo": {
                                                "hasNextPage": False,
                                                "endCursor": None,
                                            },
                                        },
                                        "reviews": {
                                            "nodes": [],
                                            "pageInfo": {
                                                "hasPreviousPage": False,
                                                "startCursor": None,
                                            },
                                        },
                                    }
                                }
                            }
                        },
                        {
                            "data": {
                                "node": {
                                    "reactions": {
                                        "nodes": [
                                            {
                                                "content": content,
                                                "user": {"login": "chatgpt-codex-connector"},
                                            }
                                        ],
                                        "pageInfo": {
                                            "hasPreviousPage": False,
                                            "startCursor": None,
                                        },
                                    }
                                }
                            }
                        },
                    ]
                )

                detail = GitHubClient(runner).detail(
                    "AnikaWilliams/example", 42, requested_comment_id="101"
                )
                classification = classify_pr(detail, requested_head=HEAD, requested_comment_id="101")

                self.assertEqual(classification.kind, expected_kind)
                self.assertIn("commentId=request-node-id", runner.json_calls[1])
                self.assertIn("before=request-reactions-100", runner.json_calls[1])
                self.assertNotIn("must-not-fetch", runner.json_calls[1])


class ControllerSafetyTests(unittest.TestCase):
    def test_repository_storage_slug_preserves_component_boundaries(self) -> None:
        first = repository_storage_slug("a/b__c")
        second = repository_storage_slug("a__b/c")

        self.assertNotEqual(first, second)
        self.assertEqual(first, repository_storage_slug("A/B__C"))
        self.assertRegex(first, r"^[a-z2-7]+$")

    def test_live_cli_flag_is_rejected_without_constructing_controller(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps(valid_config()), encoding="utf-8")
            lock_path = Path(directory) / "controller.lock"
            lock_path.write_text(str(os.getpid()), encoding="utf-8")
            with (
                patch.dict(os.environ, {"PR_AUTOPILOT_LOCK": str(lock_path)}),
                patch("pr_autopilot.Controller") as controller,
            ):
                result = main(["--config", str(config_path), "--fail-if-busy"])

        self.assertEqual(result, 2)
        controller.assert_not_called()

    def test_running_fix_verified_push_extends_runtime_and_rebinds_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            new_head = "b" * 40
            controller = Controller(
                Config.load(path),
                runner=ProgressPushRunner(new_head),  # type: ignore[arg-type]
            )
            state = PRState(
                repository="AnikaWilliams/example",
                number=42,
                updated_at="2026-08-10T00:00:00Z",
                head_sha=HEAD,
                active_task_id="t_verify",
                active_task_status="running",
                pipeline_json='{"analyze":"t_analyze","fix":"t_fix","verify":"t_verify"}',
                pending_finding_fingerprint="pending-finding-fingerprint",
                pending_findings_json='[{"body":"Keep this pending finding."}]',
            )

            with patch.object(
                controller.kanban,
                "extend_runtime",
                return_value={"applied": True, "new_limit_seconds": 14_400},
            ) as extend:
                updated, event = controller._accept_fix_progress_push(
                    state,
                    current_head=new_head,
                    pipeline_statuses={"analyze": "done", "fix": "running", "verify": "todo"},
                    dry_run=False,
                )

            self.assertEqual(updated.head_sha, HEAD)
            self.assertEqual(updated.pending_finding_fingerprint, "pending-finding-fingerprint")
            self.assertEqual(updated.pending_findings_json, '[{"body":"Keep this pending finding."}]')
            self.assertIn("extended Fix runtime", event.message if event else "")
            extend.assert_called_once_with("t_fix", verified_head=new_head)

    def test_completed_fix_exact_run_rebinds_verified_descendant_without_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            new_head = "b" * 40
            controller = Controller(
                Config.load(path),
                runner=ProgressPushRunner(new_head),  # type: ignore[arg-type]
            )
            state = PRState(
                repository="AnikaWilliams/example",
                number=42,
                updated_at="2026-08-10T00:00:00Z",
                head_sha=HEAD,
                active_task_id="t_verify",
                active_task_status="running",
                pipeline_json='{"analyze":"t_analyze","fix":"t_fix","verify":"t_verify"}',
                pending_finding_fingerprint="pending-finding-fingerprint",
                pending_findings_json='[{"body":"Keep this pending finding."}]',
            )
            fix_details = {
                "task": {
                    "id": "t_fix",
                    "status": "done",
                    "assignee": "prfix",
                    "tenant": "pr-autopilot:AnikaWilliams/example#42",
                },
                "runs": [
                    {
                        "id": 7,
                        "profile": "prfix",
                        "outcome": "completed",
                        "metadata": {"commit_sha": new_head, "pushed_to": "feature/fix"},
                    }
                ],
            }

            with (
                patch.object(controller.kanban, "details", return_value=fix_details),
                patch.object(controller.kanban, "extend_runtime") as extend,
            ):
                updated, event = controller._accept_fix_progress_push(
                    state,
                    current_head=new_head,
                    pipeline_statuses={"analyze": "done", "fix": "done", "verify": "running"},
                    dry_run=False,
                )

            self.assertEqual(updated.head_sha, new_head)
            self.assertEqual(updated.pending_finding_fingerprint, "pending-finding-fingerprint")
            self.assertEqual(updated.pending_findings_json, '[{"body":"Keep this pending finding."}]')
            self.assertIn("completed Fix", event.message if event else "")
            extend.assert_not_called()

    def test_running_fix_progress_can_later_retire_the_changed_head_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            new_head = "b" * 40
            summary = PRSummary(
                repository="AnikaWilliams/example",
                number=42,
                title="Example",
                url="https://github.com/AnikaWilliams/example/pull/42",
                updated_at="2026-08-11T00:00:00Z",
                is_draft=False,
            )
            detail = {
                "headRefOid": new_head,
                "headRefName": "feature/fix-progress",
                "headRepository": {"nameWithOwner": summary.repository},
                "isDraft": False,
                "labels": {"nodes": []},
                "comments": {"nodes": []},
                "reviews": {"nodes": []},
            }
            pipeline = {"analyze": "t_analyze", "fix": "t_fix", "verify": "t_verify"}
            starting_state = PRState(
                repository=summary.repository,
                number=summary.number,
                updated_at="2026-08-10T00:00:00Z",
                head_sha=HEAD,
                active_task_id="t_verify",
                active_task_status="running",
                pipeline_json=json.dumps(pipeline),
                policy_revision=1,
            )
            controller.store.save(starting_state)

            with (
                patch.object(controller.github, "authored_open_prs", return_value=[summary]),
                patch.object(controller.github, "detail", return_value=detail),
                patch.object(controller, "_eligible", return_value=True),
                patch.object(controller, "_source_repository", return_value=summary.repository),
                patch.object(
                    controller,
                    "_observe_pipeline",
                    return_value=(
                        starting_state,
                        False,
                        {"analyze": "done", "fix": "running", "verify": "scheduled"},
                    ),
                ),
                patch.object(
                    controller,
                    "_accept_fix_progress_push",
                    return_value=(
                        starting_state,
                        pr_autopilot.Event(summary.repository, summary.number, "extended Fix runtime"),
                    ),
                ),
                patch.object(controller.kanban, "retire_pipeline", create=True) as retire_pipeline,
                patch.object(controller.github, "request_codex_review") as request_codex_review,
            ):
                first_events = controller.run(dry_run=False, verbose=False)

            after_progress = controller.store.load(summary.repository, summary.number)
            self.assertEqual(after_progress.head_sha if after_progress else None, HEAD)
            self.assertEqual(after_progress.active_task_status if after_progress else None, "running")
            retire_pipeline.assert_not_called()
            request_codex_review.assert_not_called()
            self.assertTrue(any("extended Fix runtime" in event.message for event in first_events))

            assert after_progress is not None
            blocked_state = replace(
                after_progress,
                active_task_status="blocked",
                updated_at="2026-08-11T00:01:00Z",
            )
            controller.store.save(blocked_state)
            with (
                patch.object(controller.github, "authored_open_prs", return_value=[summary]),
                patch.object(controller.github, "detail", return_value=detail),
                patch.object(controller, "_eligible", return_value=True),
                patch.object(controller, "_source_repository", return_value=summary.repository),
                patch.object(
                    controller,
                    "_accept_fix_progress_push",
                    return_value=(blocked_state, None),
                ),
                patch.object(controller.kanban, "retire_pipeline", create=True) as retire_pipeline,
                patch.object(
                    controller.github,
                    "request_codex_review",
                    return_value="fresh-review-request",
                ) as request_codex_review,
            ):
                second_events = controller.run(dry_run=False, verbose=False)

            retire_pipeline.assert_called_once_with(
                pipeline,
                reason="retired after the pull request head changed",
                cancel_running=True,
            )
            request_codex_review.assert_called_once_with(summary.repository, summary.number)
            after_block = controller.store.load(summary.repository, summary.number)
            self.assertEqual(after_block.head_sha if after_block else None, new_head)
            self.assertIsNone(after_block.pipeline_json if after_block else "missing")
            self.assertTrue(
                any(
                    "retired old-head pipeline; requested fresh Codex review" in event.message
                    for event in second_events
                )
            )

    def test_completed_fix_rejects_a_mismatched_latest_run_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            new_head = "b" * 40
            controller = Controller(
                Config.load(path),
                runner=ProgressPushRunner(new_head),  # type: ignore[arg-type]
            )
            state = PRState(
                repository="AnikaWilliams/example",
                number=42,
                updated_at="2026-08-10T00:00:00Z",
                head_sha=HEAD,
                active_task_id="t_verify",
                active_task_status="running",
                pipeline_json='{"analyze":"t_analyze","fix":"t_fix","verify":"t_verify"}',
            )
            fix_details = {
                "task": {
                    "id": "t_fix",
                    "status": "done",
                    "assignee": "prfix",
                    "tenant": "pr-autopilot:AnikaWilliams/example#42",
                },
                "runs": [
                    {
                        "id": 7,
                        "profile": "prfix",
                        "outcome": "completed",
                        "metadata": {"commit_sha": new_head},
                    },
                    {
                        "id": 8,
                        "profile": "prfix",
                        "outcome": "completed",
                        "metadata": {"commit_sha": HEAD},
                    },
                ],
            }

            with (
                patch.object(controller.kanban, "details", return_value=fix_details),
                patch.object(controller.kanban, "extend_runtime") as extend,
            ):
                updated, event = controller._accept_fix_progress_push(
                    state,
                    current_head=new_head,
                    pipeline_statuses={"analyze": "done", "fix": "done", "verify": "running"},
                    dry_run=False,
                )

            self.assertEqual(updated.head_sha, HEAD)
            self.assertIsNone(event)
            extend.assert_not_called()

    def test_completed_fix_rejects_wrong_tenant_or_worker_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            new_head = "b" * 40
            controller = Controller(
                Config.load(path),
                runner=ProgressPushRunner(new_head),  # type: ignore[arg-type]
            )
            state = PRState(
                repository="AnikaWilliams/example",
                number=42,
                updated_at="2026-08-10T00:00:00Z",
                head_sha=HEAD,
                active_task_id="t_verify",
                active_task_status="running",
                pipeline_json='{"analyze":"t_analyze","fix":"t_fix","verify":"t_verify"}',
            )
            for task_overrides, run_overrides in (
                ({"tenant": "pr-autopilot:AnikaWilliams/other#42"}, {}),
                ({}, {"profile": "other-worker"}),
            ):
                with self.subTest(task_overrides=task_overrides, run_overrides=run_overrides):
                    task = {
                        "id": "t_fix",
                        "status": "done",
                        "assignee": "prfix",
                        "tenant": "pr-autopilot:AnikaWilliams/example#42",
                        **task_overrides,
                    }
                    run = {
                        "id": 7,
                        "profile": "prfix",
                        "outcome": "completed",
                        "metadata": {"commit_sha": new_head},
                        **run_overrides,
                    }
                    with (
                        patch.object(controller.kanban, "details", return_value={"task": task, "runs": [run]}),
                        patch.object(controller.kanban, "extend_runtime") as extend,
                    ):
                        updated, event = controller._accept_fix_progress_push(
                            state,
                            current_head=new_head,
                            pipeline_statuses={"analyze": "done", "fix": "done", "verify": "running"},
                            dry_run=False,
                        )

                    self.assertEqual(updated.head_sha, HEAD)
                    self.assertIsNone(event)
                    extend.assert_not_called()

    def test_active_pipeline_bypasses_unchanged_summary_fast_path_for_completed_fix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            new_head = "b" * 40
            summary = {
                "repository": {"nameWithOwner": "AnikaWilliams/example"},
                "number": 42,
                "title": "Example",
                "url": "https://github.com/AnikaWilliams/example/pull/42",
                "updatedAt": "2026-08-10T00:00:00Z",
                "isDraft": False,
            }
            detail = {
                "headRefOid": new_head,
                "headRefName": "feature/fix",
                "headRepository": {"nameWithOwner": "AnikaWilliams/example"},
                "isDraft": False,
                "labels": {"nodes": []},
                "comments": {"nodes": []},
                "reviews": {"nodes": []},
            }
            fix_details = {
                "task": {
                    "id": "t_fix",
                    "status": "done",
                    "assignee": "prfix",
                    "tenant": "pr-autopilot:AnikaWilliams/example#42",
                },
                "runs": [
                    {
                        "id": 7,
                        "profile": "prfix",
                        "outcome": "completed",
                        "metadata": {"commit_sha": new_head},
                    }
                ],
            }
            runner = ProgressPushRunner(new_head)
            runner.responses = [
                [summary],
                {"data": {"repository": {"pullRequest": detail}}},
                {"task": {"status": "done"}},
                {"task": {"status": "done"}},
                {"task": {"status": "running"}},
                fix_details,
            ]
            controller = Controller(Config.load(path), runner=runner)  # type: ignore[arg-type]
            controller.store.save(
                PRState(
                    repository="AnikaWilliams/example",
                    number=42,
                    updated_at=summary["updatedAt"],
                    head_sha=HEAD,
                    policy_revision=1,
                    active_task_id="t_verify",
                    active_task_status="running",
                    pipeline_json='{"analyze":"t_analyze","fix":"t_fix","verify":"t_verify"}',
                )
            )

            events = controller.run(dry_run=False, verbose=False)

            updated = controller.store.load("AnikaWilliams/example", 42)
            self.assertEqual(updated.head_sha if updated else None, new_head)
            self.assertTrue(any("accepted completed Fix push" in event.message for event in events))
            self.assertTrue(any("graphql" in call for call in runner.json_calls))

    def test_fix_progress_push_rejects_remote_head_not_checked_out_locally(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(
                Config.load(path),
                runner=ProgressPushRunner(HEAD),  # type: ignore[arg-type]
            )
            state = PRState(
                repository="AnikaWilliams/example",
                number=42,
                updated_at="2026-08-10T00:00:00Z",
                head_sha=HEAD,
                active_task_id="t_verify",
                active_task_status="running",
                pipeline_json='{"fix":"t_fix","verify":"t_verify"}',
            )

            with patch.object(controller.kanban, "extend_runtime") as extend:
                updated, event = controller._accept_fix_progress_push(
                    state,
                    current_head="b" * 40,
                    pipeline_statuses={"fix": "running", "verify": "todo"},
                    dry_run=False,
                )

            self.assertEqual(updated.head_sha, HEAD)
            self.assertIsNone(event)
            extend.assert_not_called()

    def test_fix_progress_push_runtime_extension_failure_is_a_safe_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            new_head = "b" * 40
            controller = Controller(
                Config.load(path),
                runner=ProgressPushRunner(new_head),  # type: ignore[arg-type]
            )
            state = PRState(
                repository="AnikaWilliams/example",
                number=42,
                updated_at="2026-08-10T00:00:00Z",
                head_sha=HEAD,
                pipeline_json='{"fix":"t_fix","verify":"t_verify"}',
            )

            with patch.object(
                controller.kanban,
                "extend_runtime",
                side_effect=CommandError("Fix card stopped before extension"),
            ):
                updated, event = controller._accept_fix_progress_push(
                    state,
                    current_head=new_head,
                    pipeline_statuses={"fix": "running", "verify": "todo"},
                    dry_run=False,
                )

            self.assertEqual(updated.head_sha, HEAD)
            self.assertIsNone(event)

    def test_cli_dry_run_does_not_create_state_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            state_path = Path(temporary_directory) / "state" / "pr-autopilot.sqlite3"

            with redirect_stderr(StringIO()), patch.object(
                Controller, "run", return_value=[]
            ):
                exit_code = main(["--config", str(path), "--dry-run"])

            self.assertEqual(exit_code, 1)
            self.assertFalse(state_path.exists())
            self.assertFalse(state_path.parent.exists())

    def test_provider_probe_uses_an_installed_no_redirect_handler(self) -> None:
        """The HTTPS recovery opener must use the redirect-rejecting handler."""
        handler = next(
            (
                candidate
                for candidate in _NO_REDIRECT_OPENER.handlers
                if isinstance(candidate, _NoRedirectHandler)
            ),
            None,
        )

        self.assertIsNotNone(handler)
        self.assertIsNone(
            handler.redirect_request(  # type: ignore[union-attr]
                request=object(),
                fp=None,
                code=302,
                msg="Found",
                headers={},
                newurl="http://attacker.example/models",
            )
        )

    def test_network_recovery_rejects_noncanonical_trailing_dot_host(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            payload = {
                "task": {"status": "blocked", "block_kind": "transient"},
                "runs": [
                    {
                        "outcome": "provider_unavailable",
                        "metadata": {
                            "failure_reason": "timeout",
                            "endpoint": "https://spark.example.test./v1",
                        },
                    }
                ],
            }

            self.assertIsNone(controller._network_recovery_endpoint(payload))

    def test_network_recovery_skips_a_durably_exhausted_transient_task(self) -> None:
        """A restarted controller must not relaunch an exhausted task."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            payload = {
                "task": {"status": "blocked", "block_kind": "transient"},
                "runs": [
                    {
                        "outcome": "provider_unavailable",
                        "metadata": {
                            "failure_reason": "overloaded",
                            "endpoint": "https://spark.example.test/v1/",
                            "automatic_recovery_count": 1,
                        },
                    }
                ],
            }

            self.assertIsNone(controller._network_recovery_endpoint(payload))

    def test_provider_probe_requires_authenticated_expected_model(self) -> None:
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {"data": [{"id": "dgx-model"}], "object": "list"}
                ).encode("utf-8")

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            seen: list[object] = []

            def fake_urlopen(request, *, timeout: int):
                seen.extend([request, timeout])
                return FakeResponse()

            with patch.dict("os.environ", {"TEST_RECOVERY_API_KEY": "secret"}), patch(
                "pr_autopilot._NO_REDIRECT_OPENER.open", side_effect=fake_urlopen
            ):
                self.assertTrue(
                    controller._probe_https_endpoint("https://spark.example.test/v1")
                )

            request = seen[0]
            self.assertEqual(request.full_url, "https://spark.example.test/v1/models")
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.get_header("Authorization"), "Bearer secret")
            self.assertEqual(seen[1], 5)

    def test_provider_probe_rejects_unauthorized_or_missing_model(self) -> None:
        class MissingModelResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"data":[{"id":"other-model"}]}'

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            unauthorized = HTTPError(
                "https://spark.example.test/v1/models", 401, "unauthorized", {}, None
            )

            with patch.dict("os.environ", {}, clear=True), patch(
                "pr_autopilot._NO_REDIRECT_OPENER.open"
            ) as mocked:
                self.assertFalse(
                    controller._probe_https_endpoint("https://spark.example.test/v1")
                )
                mocked.assert_not_called()

            with patch.dict("os.environ", {"TEST_RECOVERY_API_KEY": "secret"}), patch(
                "pr_autopilot._NO_REDIRECT_OPENER.open", side_effect=unauthorized
            ):
                self.assertFalse(
                    controller._probe_https_endpoint("https://spark.example.test/v1")
                )

            with patch.dict("os.environ", {"TEST_RECOVERY_API_KEY": "secret"}), patch(
                "pr_autopilot._NO_REDIRECT_OPENER.open", return_value=MissingModelResponse()
            ):
                self.assertFalse(
                    controller._probe_https_endpoint("https://spark.example.test/v1")
                )

    def test_generated_analysis_body_enforces_twelve_step_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            summary = PRSummary(
                repository="AnikaWilliams/example",
                number=42,
                title="Example",
                url="https://github.com/AnikaWilliams/example/pull/42",
                updated_at="2026-08-11T00:00:00Z",
                is_draft=False,
            )
            body = controller._analysis_body(
                summary=summary,
                detail={"headRefOid": HEAD, "headRefName": "feature/fix"},
                worktree=Path(temporary_directory) / "worktree",
                findings=({"databaseId": 99, "body": "Analyze it.", "path": "a.py"},),
            )

            self.assertIn("no more than 12 investigation steps", body)
            self.assertIn("Never investigate until timeout", body)

    def test_generated_fix_body_enforces_bounded_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            summary = PRSummary(
                repository="AnikaWilliams/example",
                number=42,
                title="Example",
                url="https://github.com/AnikaWilliams/example/pull/42",
                updated_at="2026-08-11T00:00:00Z",
                is_draft=False,
            )
            body = controller._task_body(
                summary=summary,
                detail={"headRefOid": HEAD, "headRefName": "feature/fix"},
                worktree=Path(temporary_directory) / "worktree",
                findings=({"databaseId": 99, "body": "Fix it.", "path": "a.py"},),
            )

            self.assertIn("six investigation steps", body)
            self.assertIn("one focused test", body)
            self.assertIn("one typecheck or lint", body)
            self.assertIn("Block before the runtime limit", body)

    def test_new_findings_during_active_pipeline_are_commented_once_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            runner = RecordingRunner(
                [{"task": {"id": "t_fix"}, "comments": []}]
            )
            controller = Controller(Config.load(path), runner=runner)  # type: ignore[arg-type]
            findings = (
                {
                    "databaseId": 99,
                    "body": "New exact-head bug.",
                    "path": "src/example.py",
                    "line": 12,
                },
            )
            fingerprint = findings_fingerprint(HEAD, findings)
            state = PRState(
                repository="AnikaWilliams/example",
                number=42,
                updated_at="2026-08-11T00:00:00Z",
                head_sha=HEAD,
                active_task_id="t_verify",
                active_task_status="blocked",
                last_finding_fingerprint="old-fingerprint",
                pipeline_json='{"analyze":"t_analyze","fix":"t_fix","verify":"t_verify"}',
            )
            result = ClassificationResult(
                Classification.FINDINGS,
                reviewed_sha=HEAD,
                findings=findings,
            )

            updated, event = controller._surface_pending_findings(
                state,
                result=result,
                fingerprint=fingerprint,
                pipeline_statuses={
                    "analyze": "done",
                    "fix": "blocked",
                    "verify": "todo",
                },
                dry_run=False,
            )
            repeated, repeated_event = controller._surface_pending_findings(
                updated,
                result=result,
                fingerprint=fingerprint,
                pipeline_statuses={
                    "analyze": "done",
                    "fix": "blocked",
                    "verify": "todo",
                },
                dry_run=False,
            )

            self.assertEqual(updated.pending_finding_fingerprint, fingerprint)
            self.assertIn("New exact-head bug", updated.pending_findings_json or "")
            self.assertIsNotNone(event)
            self.assertEqual(repeated, updated)
            self.assertIsNone(repeated_event)
            comment_calls = [
                call for call in runner.run_calls
                if call[:3] == ["hermes", "kanban", "comment"]
            ]
            self.assertEqual(len(comment_calls), 1)
            self.assertEqual(comment_calls[0][3], "t_fix")
            self.assertIn(fingerprint, " ".join(comment_calls[0]))

    def test_overlapping_pending_findings_comment_only_the_new_finding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            runner = RecordingRunner(
                [
                    {"task": {"id": "t_fix"}, "comments": []},
                    {"task": {"id": "t_fix"}, "comments": []},
                ]
            )
            controller = Controller(Config.load(path), runner=runner)  # type: ignore[arg-type]
            first = {"databaseId": 99, "body": "First pending finding."}
            second = {"databaseId": 100, "body": "Second pending finding."}
            state = PRState(
                repository="AnikaWilliams/example",
                number=42,
                updated_at="2026-08-11T00:00:00Z",
                head_sha=HEAD,
                active_task_id="t_fix",
                active_task_status="running",
                last_finding_fingerprint="original-pipeline",
                pipeline_json='{"fix":"t_fix","verify":"t_verify"}',
            )

            first_result = ClassificationResult(
                Classification.FINDINGS, reviewed_sha=HEAD, findings=(first,)
            )
            state, _ = controller._surface_pending_findings(
                state,
                result=first_result,
                fingerprint=findings_fingerprint(HEAD, first_result.findings),
                pipeline_statuses={"fix": "running", "verify": "todo"},
                dry_run=False,
            )
            superset = ClassificationResult(
                Classification.FINDINGS,
                reviewed_sha=HEAD,
                findings=(first, second),
            )
            state, event = controller._surface_pending_findings(
                state,
                result=superset,
                fingerprint=findings_fingerprint(HEAD, superset.findings),
                pipeline_statuses={"fix": "running", "verify": "todo"},
                dry_run=False,
            )

            comments = [
                call[4]
                for call in runner.run_calls
                if call[:3] == ["hermes", "kanban", "comment"]
            ]
            self.assertEqual(len(comments), 2)
            self.assertIn("First pending finding", comments[0])
            self.assertNotIn("First pending finding", comments[1])
            self.assertIn("Second pending finding", comments[1])
            self.assertIn("First pending finding", state.pending_findings_json or "")
            self.assertIn("Second pending finding", state.pending_findings_json or "")
            self.assertIsNotNone(event)

    def test_terminal_pipeline_consumes_persisted_pending_findings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            finding = {"databaseId": 99, "body": "Persisted pending finding."}
            current_finding = {"databaseId": 100, "body": "Current review finding."}
            state = PRState(
                repository="AnikaWilliams/example",
                number=42,
                updated_at="2026-08-11T00:00:00Z",
                head_sha=HEAD,
                active_task_id="t_verify",
                active_task_status="done",
                pending_finding_fingerprint=findings_fingerprint(HEAD, (finding,)),
                pending_findings_json=json.dumps([finding]),
            )

            consumed = controller._consume_terminal_pending_findings(
                state,
                ClassificationResult(
                    Classification.FINDINGS,
                    reviewed_sha=HEAD,
                    findings=(finding, current_finding),
                ),
            )

            self.assertEqual(consumed.kind, Classification.FINDINGS)
            self.assertEqual(consumed.findings, (finding, current_finding))

    def test_comment_failure_does_not_abort_later_prs_in_the_sweep(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            runner = FirstCommentFailsRunner(
                [
                    {"task": {"id": "t_fix_1"}, "comments": []},
                    {"task": {"id": "t_fix_2"}, "comments": []},
                ]
            )
            controller = Controller(Config.load(path), runner=runner)  # type: ignore[arg-type]
            summaries = [
                PRSummary(
                    repository="AnikaWilliams/example",
                    number=number,
                    title=f"PR {number}",
                    url=f"https://github.com/AnikaWilliams/example/pull/{number}",
                    updated_at="2026-08-11T01:00:00Z",
                    is_draft=False,
                )
                for number in (1, 2)
            ]
            finding = {"databaseId": 99, "body": "New pending finding."}
            detail = {
                "headRefOid": HEAD,
                "headRepository": {"nameWithOwner": "AnikaWilliams/example"},
                "reviews": {
                    "nodes": [
                        {
                            "author": {"login": "chatgpt-codex-connector"},
                            "body": f"Reviewed commit: `{HEAD}`",
                            "comments": {"nodes": [finding]},
                        }
                    ]
                },
                "comments": {"nodes": []},
                "labels": {"nodes": []},
            }
            for number in (1, 2):
                controller.store.save(
                    PRState(
                        repository="AnikaWilliams/example",
                        number=number,
                        updated_at="2026-08-11T00:00:00Z",
                        head_sha=HEAD,
                        policy_revision=1,
                        active_task_id=f"t_verify_{number}",
                        active_task_status="running",
                        last_finding_fingerprint="original-pipeline",
                        pipeline_json=json.dumps(
                            {"fix": f"t_fix_{number}", "verify": f"t_verify_{number}"}
                        ),
                    )
                )

            with (
                patch.object(controller.github, "authored_open_prs", return_value=summaries),
                patch.object(controller.github, "detail", side_effect=[detail, detail]),
                patch.object(controller, "_eligible", return_value=True),
                patch.object(controller, "_source_repository", return_value="AnikaWilliams/example"),
                patch.object(
                    controller,
                    "_observe_pipeline",
                    side_effect=lambda state: (
                        state,
                        False,
                        {"fix": "running", "verify": "todo"},
                    ),
                ),
            ):
                events = controller.run(dry_run=False, verbose=False)

            first_state = controller.store.load("AnikaWilliams/example", 1)
            second_state = controller.store.load("AnikaWilliams/example", 2)
            self.assertIsNone(first_state.pending_findings_json if first_state else None)
            self.assertIn("New pending finding", second_state.pending_findings_json or "")
            self.assertTrue(any("remain unreconciled" in event.message for event in events))

    def test_nonterminal_pipeline_outside_the_discovery_limit_is_still_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(
                json.dumps({**valid_config(), "max_open_prs": 1}), encoding="utf-8"
            )
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            tracked = PRState(
                repository="AnikaWilliams/example",
                number=42,
                updated_at="2026-08-10T00:00:00Z",
                head_sha=HEAD,
                policy_revision=1,
                requested_head=HEAD,
                active_task_id="t_verify",
                active_task_status="running",
                pipeline_json='{"analyze":"t_analyze","fix":"t_fix","verify":"t_verify"}',
            )
            controller.store.save(tracked)
            newer_untracked = PRSummary(
                repository="AnikaWilliams/newer",
                number=99,
                title="Newer pull request",
                url="https://github.com/AnikaWilliams/newer/pull/99",
                updated_at="2026-08-11T00:00:00Z",
                is_draft=False,
            )
            detail = {
                "state": "OPEN",
                "headRefOid": HEAD,
                "headRefName": "feature/tracked",
                "headRepository": {"nameWithOwner": tracked.repository},
                "title": "Tracked pull request",
                "url": "https://github.com/AnikaWilliams/example/pull/42",
                "updatedAt": "2026-08-12T00:00:00Z",
                "isDraft": False,
                "labels": {"nodes": []},
                "comments": {"nodes": []},
                "reviews": {"nodes": []},
            }

            with (
                patch.object(
                    controller.github, "authored_open_prs", return_value=[newer_untracked]
                ) as discover,
                patch.object(controller.github, "detail", return_value=detail) as get_detail,
                patch.object(controller, "_eligible", return_value=True),
                patch.object(controller, "_source_repository", return_value=tracked.repository),
                patch.object(
                    controller,
                    "_observe_pipeline",
                    return_value=(
                        tracked,
                        False,
                        {"analyze": "done", "fix": "done", "verify": "running"},
                    ),
                ) as observe_pipeline,
                patch.object(controller.github, "request_codex_review") as request_review,
                patch.object(controller.workspaces, "ensure") as ensure_worktree,
                patch.object(controller.kanban, "create_pipeline") as create_pipeline,
            ):
                controller.run(dry_run=False, verbose=False)

            discover.assert_called_once_with(1)
            get_detail.assert_called_once_with(tracked.repository, tracked.number)
            observe_pipeline.assert_called_once_with(tracked)
            request_review.assert_not_called()
            ensure_worktree.assert_not_called()
            create_pipeline.assert_not_called()
            self.assertIsNotNone(
                controller.store.load(newer_untracked.repository, newer_untracked.number)
            )

    def test_pending_review_outside_the_discovery_limit_merges_its_exact_head(self) -> None:
        """A durable review request is reconciled without admitting extra new PRs."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(
                json.dumps({**valid_config(), "max_open_prs": 1}), encoding="utf-8"
            )
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            tracked = PRState(
                repository="AnikaWilliams/example",
                number=42,
                updated_at="2026-08-10T00:00:00Z",
                head_sha=HEAD,
                policy_revision=1,
                requested_head=HEAD,
                requested_comment_id="tracked-request",
                requested_at="2026-08-10T00:00:00Z",
                review_rounds=1,
            )
            controller.store.save(tracked)
            newer_open_prs = [
                PRSummary(
                    repository="AnikaWilliams/newer-one",
                    number=99,
                    title="Newer pull request one",
                    url="https://github.com/AnikaWilliams/newer-one/pull/99",
                    updated_at="2026-08-12T00:00:00Z",
                    is_draft=False,
                ),
                PRSummary(
                    repository="AnikaWilliams/newer-two",
                    number=100,
                    title="Newer pull request two",
                    url="https://github.com/AnikaWilliams/newer-two/pull/100",
                    updated_at="2026-08-11T00:00:00Z",
                    is_draft=False,
                ),
            ]
            detail = {
                "state": "OPEN",
                "headRefOid": HEAD,
                "headRefName": "feature/tracked",
                "headRepository": {"nameWithOwner": tracked.repository},
                "mergeStateStatus": "CLEAN",
                "labels": {"nodes": []},
                "comments": {
                    "nodes": [
                        {
                            "databaseId": "tracked-request",
                            "author": {"login": "AnikaWilliams"},
                            "body": "@codex review",
                            "createdAt": "2026-08-10T00:00:00Z",
                            "reactions": {
                                "nodes": [
                                    {
                                        "content": "THUMBS_UP",
                                        "user": {
                                            "login": "chatgpt-codex-connector[bot]"
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                },
                "reviews": {"nodes": []},
            }

            def bounded_discovery(limit: int) -> list[PRSummary]:
                self.assertEqual(limit, 1)
                return newer_open_prs[:limit]

            with (
                patch.object(
                    controller.github,
                    "authored_open_prs",
                    side_effect=bounded_discovery,
                ) as discover,
                patch.object(controller.github, "detail", return_value=detail) as get_detail,
                patch.object(controller.github, "merge_squash") as merge_squash,
                patch.object(
                    controller.github,
                    "merge_status",
                    return_value={"state": "OPEN", "headRefOid": HEAD, "mergedAt": None},
                ),
                patch.object(controller, "_publish_pending_merge_history", return_value=[]),
            ):
                events = controller.run(dry_run=False, verbose=False)

            discover.assert_called_once_with(1)
            get_detail.assert_called_once_with(
                tracked.repository,
                tracked.number,
                requested_comment_id="tracked-request",
            )
            merge_squash.assert_called_once_with(tracked.repository, tracked.number, HEAD)
            self.assertIsNotNone(
                controller.store.load(newer_open_prs[0].repository, newer_open_prs[0].number)
            )
            self.assertIsNone(
                controller.store.load(newer_open_prs[1].repository, newer_open_prs[1].number)
            )
            self.assertTrue(
                any("waiting for GitHub exact-head confirmation" in event.message for event in events)
            )

    def test_restart_adopts_a_posted_review_request_after_the_state_write_crashes(self) -> None:
        """A POST-success crash must not duplicate a durable review request."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            summary = PRSummary(
                repository="AnikaWilliams/example",
                number=42,
                title="Example change",
                url="https://github.com/AnikaWilliams/example/pull/42",
                updated_at="2026-08-10T20:01:00Z",
                is_draft=False,
            )
            first_controller = Controller(
                Config.load(path), runner=RecordingRunner([])
            )  # type: ignore[arg-type]
            first_controller.store.save(
                PRState(
                    repository=summary.repository,
                    number=summary.number,
                    updated_at="2026-08-10T20:00:00Z",
                    head_sha=HEAD,
                    policy_revision=1,
                )
            )
            detail_before_post = {
                "headRefOid": HEAD,
                "headRefName": "feature/example",
                "headRepository": {"nameWithOwner": summary.repository},
                "isDraft": False,
                "labels": {"nodes": []},
                "comments": {"nodes": []},
                "reviews": {"nodes": []},
            }
            posted_comment_id = "already-posted-request"
            original_save = first_controller._save

            def crash_before_comment_id_is_saved(state: PRState, dry_run: bool) -> None:
                if state.requested_comment_id == posted_comment_id:
                    raise RuntimeError("simulated crash after successful review POST")
                original_save(state, dry_run)

            with (
                patch.object(first_controller.github, "authored_open_prs", return_value=[summary]),
                patch.object(first_controller.github, "detail", return_value=detail_before_post),
                patch.object(first_controller, "_eligible", return_value=True),
                patch.object(first_controller.github, "prepare_codex_review_request") as prepare_review,
                patch.object(
                    first_controller.github,
                    "request_codex_review",
                    return_value=posted_comment_id,
                ) as post_review,
                patch.object(first_controller, "_save", side_effect=crash_before_comment_id_is_saved),
                self.assertRaisesRegex(RuntimeError, "successful review POST"),
            ):
                first_controller.run(dry_run=False, verbose=False)

            pending = first_controller.store.load(summary.repository, summary.number)
            self.assertIsNotNone(pending)
            self.assertEqual(pending.requested_head if pending else None, HEAD)
            self.assertIsNone(pending.requested_comment_id if pending else "missing")
            self.assertEqual(pending.review_rounds if pending else None, 1)
            request_token = pending.review_request_token if pending else None
            self.assertIsInstance(request_token, str)
            self.assertTrue(request_token)
            prepare_review.assert_called_once_with(request_token)
            post_review.assert_called_once_with(summary.repository, summary.number)

            restart = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            detail_after_restart = {
                **detail_before_post,
                "state": "OPEN",
                "comments": {
                    "nodes": [
                        {
                            "databaseId": posted_comment_id,
                            "author": {"login": "AnikaWilliams"},
                            "body": (
                                "@codex review\n"
                                f"<!-- pr-autopilot-request:{request_token} -->"
                            ),
                            "createdAt": summary.updated_at,
                            "reactions": {"nodes": []},
                        }
                    ]
                },
            }

            with (
                patch.object(restart.github, "authored_open_prs", return_value=[summary]),
                patch.object(restart.github, "detail", return_value=detail_after_restart) as get_detail,
                patch.object(restart, "_eligible", return_value=True),
                patch.object(restart.github, "request_codex_review") as duplicate_post,
            ):
                restart.run(dry_run=False, verbose=False)

            get_detail.assert_called_once_with(summary.repository, summary.number)
            duplicate_post.assert_not_called()
            adopted = restart.store.load(summary.repository, summary.number)
            self.assertEqual(adopted.requested_head if adopted else None, HEAD)
            self.assertEqual(adopted.requested_comment_id if adopted else None, posted_comment_id)
            self.assertIsNone(adopted.review_request_token if adopted else "missing")
            self.assertEqual(adopted.review_rounds if adopted else None, 1)

    def test_closed_or_merged_synthetic_pipeline_is_archived_without_new_actions(self) -> None:
        for pull_request_state in ("CLOSED", "MERGED"):
            with (
                self.subTest(pull_request_state=pull_request_state),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                path = Path(temporary_directory) / "config.json"
                path.write_text(json.dumps(valid_config()), encoding="utf-8")
                controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
                tracked = PRState(
                    repository="AnikaWilliams/example",
                    number=42,
                    updated_at="2026-08-10T00:00:00Z",
                    head_sha=HEAD,
                    policy_revision=1,
                    active_task_id="t_verify",
                    # This durable summary can lag the active runtime row.
                    # Closure still has to request worker cancellation first.
                    active_task_status="scheduled",
                    pipeline_json='{"analyze":"t_analyze","fix":"t_fix","verify":"t_verify"}',
                )
                controller.store.save(tracked)
                detail = {
                    "state": pull_request_state,
                    "headRefOid": HEAD,
                    "headRefName": "feature/tracked",
                    "headRepository": {"nameWithOwner": tracked.repository},
                    "title": "Closed tracked pull request",
                    "url": "https://github.com/AnikaWilliams/example/pull/42",
                    "updatedAt": "2026-08-12T00:00:00Z",
                    "isDraft": False,
                    "labels": {"nodes": []},
                    "comments": {"nodes": []},
                    "reviews": {"nodes": []},
                }

                with (
                    patch.object(controller.github, "authored_open_prs", return_value=[]),
                    patch.object(controller.github, "detail", return_value=detail),
                    patch.object(controller.kanban, "retire_pipeline") as retire_pipeline,
                    patch.object(controller.github, "request_codex_review") as request_review,
                    patch.object(controller, "_observe_pipeline") as observe_pipeline,
                ):
                    controller.run(dry_run=False, verbose=False)

                retire_pipeline.assert_called_once_with(
                    tracked.pipeline_tasks(),
                    reason=f"tracked pull request is {pull_request_state.lower()}",
                    cancel_running=True,
                )
                request_review.assert_not_called()
                observe_pipeline.assert_not_called()
                archived = controller.store.load(tracked.repository, tracked.number)
                self.assertEqual(archived.active_task_status if archived else None, "archived")

    def test_synthetic_closed_pipeline_stays_active_when_worker_cancellation_is_indeterminate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            tracked = PRState(
                repository="AnikaWilliams/example",
                number=42,
                updated_at="2026-08-10T00:00:00Z",
                head_sha=HEAD,
                policy_revision=1,
                active_task_id="t_verify",
                active_task_status="scheduled",
                pipeline_json='{"analyze":"t_analyze","fix":"t_fix","verify":"t_verify"}',
            )
            controller.store.save(tracked)
            detail = {
                "state": "CLOSED",
                "headRefOid": HEAD,
                "headRefName": "feature/tracked",
                "headRepository": {"nameWithOwner": tracked.repository},
                "title": "Closed tracked pull request",
                "url": "https://github.com/AnikaWilliams/example/pull/42",
                "updatedAt": "2026-08-12T00:00:00Z",
                "isDraft": False,
                "labels": {"nodes": []},
                "comments": {"nodes": []},
                "reviews": {"nodes": []},
            }

            with (
                patch.object(controller.github, "authored_open_prs", return_value=[]),
                patch.object(controller.github, "detail", return_value=detail),
                patch.object(
                    controller.kanban,
                    "retire_pipeline",
                    side_effect=CommandError("worker cancellation is indeterminate"),
                ) as retire_pipeline,
                patch.object(controller.github, "request_codex_review") as request_review,
                patch.object(controller, "_observe_pipeline") as observe_pipeline,
            ):
                controller.run(dry_run=False, verbose=False)

            retire_pipeline.assert_called_once_with(
                tracked.pipeline_tasks(),
                reason="tracked pull request is closed",
                cancel_running=True,
            )
            request_review.assert_not_called()
            observe_pipeline.assert_not_called()
            unchanged = controller.store.load(tracked.repository, tracked.number)
            self.assertEqual(unchanged.active_task_status if unchanged else None, "scheduled")
            self.assertEqual(unchanged.pipeline_tasks() if unchanged else None, tracked.pipeline_tasks())

    def test_pipeline_creation_failure_does_not_abort_later_prs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            summaries = [
                PRSummary(
                    repository="AnikaWilliams/example",
                    number=number,
                    title=f"PR {number}",
                    url=f"https://github.com/AnikaWilliams/example/pull/{number}",
                    updated_at="2026-08-11T01:00:00Z",
                    is_draft=False,
                )
                for number in (1, 2)
            ]
            finding = {"databaseId": 99, "body": "Create a repair pipeline."}
            detail = {
                "headRefOid": HEAD,
                "headRefName": "feature/fix",
                "headRepository": {"nameWithOwner": "AnikaWilliams/example"},
                "reviews": {
                    "nodes": [
                        {
                            "author": {"login": "chatgpt-codex-connector"},
                            "body": f"Reviewed commit: `{HEAD}`",
                            "comments": {"nodes": [finding]},
                        }
                    ]
                },
                "comments": {"nodes": []},
                "labels": {"nodes": []},
            }
            for number in (1, 2):
                controller.store.save(
                    PRState(
                        repository="AnikaWilliams/example",
                        number=number,
                        updated_at="2026-08-11T00:00:00Z",
                        head_sha=HEAD,
                        policy_revision=1,
                    )
                )

            with (
                patch.object(controller.github, "authored_open_prs", return_value=summaries),
                patch.object(controller.github, "detail", side_effect=[detail, detail]),
                patch.object(controller, "_eligible", return_value=True),
                patch.object(controller, "_source_repository", return_value="AnikaWilliams/example"),
                patch.object(
                    controller,
                    "_observe_pipeline",
                    side_effect=lambda state: (state, False, {}),
                ),
                patch.object(
                    controller.workspaces,
                    "ensure",
                    return_value=Path(temporary_directory) / "worktree",
                ),
                patch.object(
                    controller.kanban,
                    "create_pipeline",
                    side_effect=[
                        CommandError("transient Kanban create failure"),
                        {"analyze": "t_a2", "fix": "t_f2", "verify": "t_v2"},
                    ],
                ),
            ):
                events = controller.run(dry_run=False, verbose=False)

            first_state = controller.store.load("AnikaWilliams/example", 1)
            second_state = controller.store.load("AnikaWilliams/example", 2)
            self.assertIsNone(first_state.active_task_id if first_state else None)
            self.assertEqual(second_state.active_task_id if second_state else None, "t_v2")
            self.assertTrue(any("pipeline creation failed" in event.message for event in events))

    def test_newly_observed_network_block_recovers_in_the_same_tick(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            summary = {
                "repository": {"nameWithOwner": "AnikaWilliams/example"},
                "number": 42,
                "title": "Example",
                "url": "https://github.com/AnikaWilliams/example/pull/42",
                "updatedAt": "2026-08-10T00:00:00Z",
                "isDraft": False,
            }
            blocked = {
                "task": {
                    "id": "t_analyze",
                    "status": "blocked",
                    "block_kind": "transient",
                },
                "runs": [
                    {
                        "id": 13,
                        "outcome": "provider_unavailable",
                        "metadata": {
                            "failure_reason": "timeout",
                            "endpoint": "https://spark.example.test/v1/",
                        },
                    }
                ],
            }
            detail = {
                "headRefOid": HEAD,
                "headRefName": "feature/fix",
                "headRepository": {"nameWithOwner": "AnikaWilliams/example"},
                "isDraft": False,
                "labels": {"nodes": []},
                "comments": {"nodes": []},
                "reviews": {"nodes": []},
            }
            runner = NetworkLogRunner(
                [
                    [summary],
                    {"data": {"repository": {"pullRequest": detail}}},
                    {"task": {"id": "t_analyze", "status": "blocked"}},
                    {"task": {"id": "t_fix", "status": "todo"}},
                    {"task": {"id": "t_verify", "status": "todo"}},
                    blocked,
                    {"task": {"id": "t_fix", "status": "todo"}, "runs": []},
                    {"task": {"id": "t_verify", "status": "todo"}, "runs": []},
                    {"task": {"id": "t_analyze", "status": "ready"}},
                    {"task": {"id": "t_fix", "status": "todo"}},
                    {"task": {"id": "t_verify", "status": "todo"}},
                ],
                "",
            )
            controller = Controller(
                Config.load(path),
                runner=runner,  # type: ignore[arg-type]
                endpoint_probe=lambda _endpoint: True,
            )
            controller.store.save(
                PRState(
                    repository="AnikaWilliams/example",
                    number=42,
                    updated_at=summary["updatedAt"],
                    head_sha=HEAD,
                    active_task_id="t_analyze",
                    active_task_status="scheduled",
                    pipeline_json='{"analyze":"t_analyze","fix":"t_fix","verify":"t_verify"}',
                    policy_revision=1,
                )
            )

            events = controller.run(dry_run=False, verbose=False)

            state = controller.store.load("AnikaWilliams/example", 42)
            self.assertEqual(state.active_task_status if state else None, "ready")
            self.assertTrue(
                any(
                    call[:4] == ["hermes", "kanban", "unblock", "t_analyze"]
                    for call in runner.run_calls
                )
            )
            self.assertTrue(
                any("resumed analyze card t_analyze" in event.message for event in events)
            )

    def test_network_blocked_card_unblocks_same_id_when_endpoint_returns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            runner = NetworkLogRunner(
                [
                    {
                        "task": {
                            "id": "t_analyze",
                            "status": "blocked",
                            "block_kind": "transient",
                        },
                        "runs": [
                            {
                                "id": 11,
                                "outcome": "provider_unavailable",
                                "metadata": {
                                    "failure_reason": "server_error",
                                    "endpoint": "https://spark.example.test/v1/",
                                },
                            }
                        ],
                    },
                    {"task": {"id": "t_fix", "status": "todo"}, "runs": []},
                    {"task": {"id": "t_verify", "status": "todo"}, "runs": []},
                ],
                "",
            )
            probed: list[str] = []
            controller = Controller(
                Config.load(path),
                runner=runner,  # type: ignore[arg-type]
                endpoint_probe=lambda endpoint: probed.append(endpoint) or True,
            )
            state = PRState(
                repository="AnikaWilliams/example",
                number=42,
                updated_at="2026-08-10T00:00:00Z",
                head_sha=HEAD,
                active_task_id="t_verify",
                active_task_status="blocked",
                pipeline_json='{"analyze":"t_analyze","fix":"t_fix","verify":"t_verify"}',
                policy_revision=1,
            )

            events = controller._recover_network_blocked_pipeline(
                state,
                current_head=HEAD,
                dry_run=False,
            )

            self.assertEqual(probed, ["https://spark.example.test/v1/"])
            self.assertTrue(
                any(
                    call[:4] == ["hermes", "kanban", "unblock", "t_analyze"]
                    for call in runner.run_calls
                )
            )
            self.assertTrue(
                any("resumed analyze card t_analyze" in event.message for event in events)
            )

    def test_network_recovery_never_unblocks_pipeline_for_stale_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            runner = NetworkLogRunner(
                [
                    {
                        "task": {
                            "id": "t_analyze",
                            "status": "blocked",
                            "block_kind": "transient",
                        },
                        "runs": [
                            {
                                "id": 11,
                                "outcome": "provider_unavailable",
                                "metadata": {
                                    "failure_reason": "server_error",
                                    "endpoint": "https://spark.example.test/v1/",
                                },
                            }
                        ],
                    }
                ],
                "",
            )
            probe_calls: list[str] = []
            controller = Controller(
                Config.load(path),
                runner=runner,  # type: ignore[arg-type]
                endpoint_probe=lambda endpoint: probe_calls.append(endpoint) or True,
            )
            state = PRState(
                repository="AnikaWilliams/example",
                number=42,
                updated_at="2026-08-10T00:00:00Z",
                head_sha=HEAD,
                active_task_id="t_analyze",
                active_task_status="blocked",
                pipeline_json='{"analyze":"t_analyze"}',
                policy_revision=1,
            )

            events = controller._recover_network_blocked_pipeline(
                state,
                current_head="b" * 40,
                dry_run=False,
            )

            self.assertEqual(events, [])
            self.assertEqual(probe_calls, [])
            self.assertFalse(
                any(
                    call[:3] == ["hermes", "kanban", "unblock"]
                    for call in runner.run_calls
                )
            )

    def test_changed_head_retires_blocked_pipeline_before_any_old_task_can_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            trace: list[str] = []
            controller = Controller(
                Config.load(path),
                runner=RecordingRunner([]),  # type: ignore[arg-type]
            )
            new_head = "b" * 40
            summary = PRSummary(
                repository="AnikaWilliams/example",
                number=42,
                title="Example",
                url="https://github.com/AnikaWilliams/example/pull/42",
                updated_at="2026-08-11T00:00:00Z",
                is_draft=False,
            )
            detail = {
                "headRefOid": new_head,
                "headRefName": "feature/force-pushed",
                "headRepository": {"nameWithOwner": "AnikaWilliams/example"},
                "isDraft": False,
                "labels": {"nodes": []},
                "comments": {"nodes": []},
                "reviews": {"nodes": []},
            }
            controller.store.save(
                PRState(
                    repository=summary.repository,
                    number=summary.number,
                    updated_at="2026-08-10T00:00:00Z",
                    head_sha=HEAD,
                    active_task_id="t_verify",
                    active_task_status="blocked",
                    pipeline_json=(
                        '{"analyze":"t_analyze","fix":"t_fix","verify":"t_verify"}'
                    ),
                    policy_revision=1,
                )
            )

            with (
                patch.object(
                    controller.github,
                    "authored_open_prs",
                    return_value=[summary],
                ),
                patch.object(
                    controller.github,
                    "detail",
                    side_effect=lambda *_args: trace.append("detail") or detail,
                ) as detail_call,
                patch.object(controller, "_eligible", return_value=True),
                patch.object(
                    controller,
                    "_source_repository",
                    return_value=summary.repository,
                ),
                patch.object(controller, "_observe_pipeline") as observe_pipeline,
                patch.object(controller.kanban, "retire_pipeline", create=True) as retire_pipeline,
                patch.object(
                    controller.github,
                    "request_codex_review",
                    return_value="fresh-review-request",
                ) as request_codex_review,
            ):
                events = controller.run(dry_run=False, verbose=False)

            state = controller.store.load(summary.repository, summary.number)
            self.assertEqual(trace, ["detail"])
            observe_pipeline.assert_not_called()
            retire_pipeline.assert_called_once_with(
                {"analyze": "t_analyze", "fix": "t_fix", "verify": "t_verify"},
                reason="retired after the pull request head changed",
                cancel_running=True,
            )
            request_codex_review.assert_called_once_with(summary.repository, summary.number)
            self.assertEqual(state.head_sha if state else None, new_head)
            self.assertEqual(state.updated_at if state else None, summary.updated_at)
            self.assertEqual(state.requested_head if state else None, new_head)
            self.assertEqual(state.requested_comment_id if state else None, "fresh-review-request")
            self.assertIsNone(state.active_task_id if state else "missing")
            self.assertIsNone(state.active_task_status if state else "missing")
            self.assertIsNone(state.pipeline_json if state else "missing")
            self.assertEqual(detail_call.call_count, 1)
            self.assertTrue(
                any("retired old-head pipeline; requested fresh Codex review" in event.message for event in events)
            )

    def test_changed_head_confirms_running_fix_cancellation_before_replacing_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            new_head = "b" * 40
            summary = PRSummary(
                repository="AnikaWilliams/example",
                number=42,
                title="Example",
                url="https://github.com/AnikaWilliams/example/pull/42",
                updated_at="2026-08-11T00:00:00Z",
                is_draft=False,
            )
            detail = {
                "headRefOid": new_head,
                "headRefName": "feature/advanced",
                "headRepository": {"nameWithOwner": summary.repository},
                "isDraft": False,
                "labels": {"nodes": []},
                "comments": {"nodes": []},
                "reviews": {"nodes": []},
            }
            pipeline = {
                "analyze": "t_analyze",
                "fix": "t_fix",
                "verify": "t_verify",
            }
            starting_state = PRState(
                repository=summary.repository,
                number=summary.number,
                updated_at="2026-08-10T00:00:00Z",
                head_sha=HEAD,
                active_task_id="t_fix",
                active_task_status="running",
                pipeline_json=json.dumps(pipeline),
                policy_revision=1,
            )
            controller.store.save(starting_state)
            trace: list[str] = []
            cancellation_confirmed = False

            def retire_running_fix(
                task_ids: dict[str, str], *, reason: str, cancel_running: bool
            ) -> None:
                nonlocal cancellation_confirmed
                self.assertEqual(task_ids, pipeline)
                self.assertEqual(reason, "retired after the pull request head changed")
                self.assertTrue(cancel_running)
                state_at_cancellation = controller.store.load(
                    summary.repository, summary.number
                )
                self.assertEqual(
                    state_at_cancellation.pipeline_tasks() if state_at_cancellation else None,
                    pipeline,
                )
                cancellation_confirmed = True
                trace.append("cancelled Fix")

            def request_fresh_review(*_args: object) -> str:
                self.assertTrue(cancellation_confirmed)
                trace.append("review")
                return "fresh-review-request"

            with (
                patch.object(controller.github, "authored_open_prs", return_value=[summary]),
                patch.object(controller.github, "detail", return_value=detail),
                patch.object(controller, "_eligible", return_value=True),
                patch.object(controller, "_source_repository", return_value=summary.repository),
                patch.object(
                    controller,
                    "_observe_pipeline",
                    side_effect=lambda state, **kwargs: (
                        trace.append(
                            "peek" if kwargs.get("allow_launch") is False else "launch"
                        )
                        or (
                            state,
                            True,
                            {
                                "analyze": "done",
                                "fix": "running",
                                "verify": "scheduled",
                            },
                        )
                    ),
                ) as observe_pipeline,
                patch.object(
                    controller.kanban,
                    "retire_pipeline",
                    create=True,
                    side_effect=retire_running_fix,
                ) as retire_pipeline,
                patch.object(
                    controller.github,
                    "request_codex_review",
                    side_effect=request_fresh_review,
                ),
            ):
                events = controller.run(dry_run=False, verbose=False)

            self.assertEqual(trace, ["peek", "cancelled Fix", "review"])
            observe_pipeline.assert_called_once_with(
                starting_state, allow_launch=False
            )
            retire_pipeline.assert_called_once_with(
                pipeline,
                reason="retired after the pull request head changed",
                cancel_running=True,
            )
            saved = controller.store.load(summary.repository, summary.number)
            self.assertEqual(saved.head_sha if saved else None, new_head)
            self.assertIsNone(saved.pipeline_json if saved else "missing")
            self.assertTrue(
                any(
                    "retired old-head pipeline; requested fresh Codex review"
                    in event.message
                    for event in events
                )
            )

    def test_pause_policy_is_checked_before_an_active_pipeline_is_observed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            summary = PRSummary(
                repository="AnikaWilliams/example",
                number=42,
                title="Example",
                url="https://github.com/AnikaWilliams/example/pull/42",
                updated_at="2026-08-11T00:00:00Z",
                is_draft=False,
            )
            changed_head = "b" * 40
            detail = {
                "headRefOid": changed_head,
                "headRefName": "feature/paused",
                "headRepository": {"nameWithOwner": summary.repository},
                "isDraft": False,
                "labels": {"nodes": [{"name": "hermes-pause"}]},
                "comments": {"nodes": []},
                "reviews": {"nodes": []},
            }
            controller.store.save(
                PRState(
                    repository=summary.repository,
                    number=summary.number,
                    updated_at="2026-08-10T00:00:00Z",
                    head_sha=HEAD,
                    active_task_id="t_verify",
                    active_task_status="scheduled",
                    pipeline_json=(
                        '{"analyze":"t_analyze","fix":"t_fix","verify":"t_verify"}'
                    ),
                    policy_revision=1,
                )
            )

            with (
                patch.object(controller.github, "authored_open_prs", return_value=[summary]),
                patch.object(controller.github, "detail", return_value=detail) as get_detail,
                patch.object(controller, "_observe_pipeline") as observe_pipeline,
            ):
                controller.run(dry_run=False, verbose=False)

            state = controller.store.load(summary.repository, summary.number)
            get_detail.assert_called_once_with(summary.repository, summary.number)
            observe_pipeline.assert_not_called()
            self.assertEqual(state.head_sha if state else None, HEAD)
            self.assertEqual(state.updated_at if state else None, summary.updated_at)

    def test_non_network_or_still_offline_block_never_unblocks(self) -> None:
        blocked = {
            "task": {
                "id": "t_analyze",
                "status": "blocked",
                "block_kind": "transient",
            },
            "runs": [
                {
                    "id": 12,
                    "outcome": "provider_unavailable",
                    "metadata": {
                        "failure_reason": "timeout",
                        "endpoint": "https://unlisted.example.test/v1/",
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            stale_runner = NetworkLogRunner(
                [blocked],
                "",
            )
            probe_calls: list[str] = []
            stale = Controller(
                Config.load(path),
                runner=stale_runner,  # type: ignore[arg-type]
                endpoint_probe=lambda endpoint: probe_calls.append(endpoint) or True,
            )
            state = PRState(
                repository="AnikaWilliams/example",
                number=42,
                updated_at="2026-08-10T00:00:00Z",
                head_sha=HEAD,
                active_task_id="t_analyze",
                active_task_status="blocked",
                pipeline_json='{"analyze":"t_analyze"}',
                policy_revision=1,
            )

            self.assertEqual(
                stale._recover_network_blocked_pipeline(
                    state,
                    current_head=HEAD,
                    dry_run=False,
                ), []
            )
            self.assertEqual(probe_calls, [])
            self.assertFalse(
                any(call[:3] == ["hermes", "kanban", "unblock"] for call in stale_runner.run_calls)
            )

            offline_runner = NetworkLogRunner(
                [
                    {
                        **blocked,
                        "runs": [
                            {
                                "id": 12,
                                "outcome": "provider_unavailable",
                                "metadata": {
                                    "failure_reason": "timeout",
                                    "endpoint": "https://down.example.test/v1/",
                                },
                            }
                        ],
                    }
                ],
                "",
            )
            offline = Controller(
                Config.load(path),
                runner=offline_runner,  # type: ignore[arg-type]
                endpoint_probe=lambda _endpoint: False,
            )
            self.assertEqual(
                offline._recover_network_blocked_pipeline(
                    state,
                    current_head=HEAD,
                    dry_run=False,
                ), []
            )
            self.assertFalse(
                any(call[:3] == ["hermes", "kanban", "unblock"] for call in offline_runner.run_calls)
            )

    def test_gateway_backend_500_never_auto_unblocks(self) -> None:
        blocked = {
            "task": {"id": "t_analyze", "status": "blocked"},
            "runs": [
                {"id": 27, "outcome": "crashed", "error": "pid 13788 not alive"}
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            runner = NetworkLogRunner(
                [blocked],
                """Initializing agent...
API call failed after 3 retries: HTTP 500: litellm.InternalServerError
Provider unreachable
Endpoint: https://spark.example.test/v1/
Hosted_vllmException - Cannot connect to host host.docker.internal:8888
Received Model Group=dgx-model
""",
            )
            controller = Controller(
                Config.load(path),
                runner=runner,  # type: ignore[arg-type]
                endpoint_probe=lambda _endpoint: True,
            )
            state = PRState(
                repository="AnikaWilliams/example",
                number=42,
                updated_at="2026-08-10T00:00:00Z",
                head_sha=HEAD,
                active_task_id="t_analyze",
                active_task_status="blocked",
                pipeline_json='{"analyze":"t_analyze"}',
                policy_revision=1,
            )

            self.assertEqual(
                controller._recover_network_blocked_pipeline(
                    state,
                    current_head=HEAD,
                    dry_run=False,
                ), []
            )
            self.assertFalse(
                any(call[:3] == ["hermes", "kanban", "unblock"] for call in runner.run_calls)
            )

    def test_non_transient_block_never_uses_provider_metadata_to_unblock(self) -> None:
        blocked = {
            "task": {
                "id": "t_analyze",
                "status": "blocked",
                "block_kind": "needs_input",
            },
            "runs": [
                {
                    "id": 28,
                    "outcome": "provider_unavailable",
                    "metadata": {
                        "failure_reason": "server_error",
                        "endpoint": "https://spark.example.test/v1/",
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            runner = NetworkLogRunner([blocked], "")
            probes: list[str] = []
            controller = Controller(
                Config.load(path),
                runner=runner,  # type: ignore[arg-type]
                endpoint_probe=lambda endpoint: probes.append(endpoint) or True,
            )
            state = PRState(
                repository="AnikaWilliams/example",
                number=42,
                updated_at="2026-08-10T00:00:00Z",
                head_sha=HEAD,
                active_task_id="t_analyze",
                active_task_status="blocked",
                pipeline_json='{"analyze":"t_analyze"}',
                policy_revision=1,
            )

            self.assertEqual(
                controller._recover_network_blocked_pipeline(
                    state,
                    current_head=HEAD,
                    dry_run=False,
                ), []
            )
            self.assertEqual(probes, [])
            self.assertFalse(
                any(call[:3] == ["hermes", "kanban", "unblock"] for call in runner.run_calls)
            )

    def test_empty_repository_opt_out_list_allows_an_unlabeled_pr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            summary = PRSummary(
                repository="AnikaWilliams/example",
                number=42,
                title="Example",
                url="https://github.com/AnikaWilliams/example/pull/42",
                updated_at="2026-08-10T00:00:00Z",
                is_draft=False,
            )

            eligible = controller._eligible(
                summary,
                {"isDraft": False, "labels": {"nodes": []}},
            )

            self.assertTrue(eligible)

    def test_repository_opt_out_denies_an_unlabeled_pr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(
                json.dumps({**valid_config(), "excluded_repositories": ["ANIKAWILLIAMS/EXAMPLE"]}),
                encoding="utf-8",
            )
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            summary = PRSummary(
                repository="AnikaWilliams/example",
                number=42,
                title="Example",
                url="https://github.com/AnikaWilliams/example/pull/42",
                updated_at="2026-08-10T00:00:00Z",
                is_draft=False,
            )

            eligible = controller._eligible(
                summary,
                {"isDraft": False, "labels": {"nodes": []}},
            )

            self.assertFalse(eligible)

    def test_pause_label_denies_an_otherwise_eligible_pr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            summary = PRSummary(
                repository="AnikaWilliams/example",
                number=42,
                title="Example",
                url="https://github.com/AnikaWilliams/example/pull/42",
                updated_at="2026-08-10T00:00:00Z",
                is_draft=False,
            )

            eligible = controller._eligible(
                summary,
                {"isDraft": False, "labels": {"nodes": [{"name": "HERMES-PAUSE"}]}},
            )

            self.assertFalse(eligible)

    def test_disabled_repository_denies_an_otherwise_eligible_pr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(
                json.dumps(
                    {**valid_config(), "disabled_repositories": ["ANIKAWILLIAMS/EXAMPLE"]}
                ),
                encoding="utf-8",
            )
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            summary = PRSummary(
                repository="AnikaWilliams/example",
                number=42,
                title="Example",
                url="https://github.com/AnikaWilliams/example/pull/42",
                updated_at="2026-08-10T00:00:00Z",
                is_draft=False,
            )

            eligible = controller._eligible(
                summary,
                {"isDraft": False, "labels": {"nodes": []}},
            )

            self.assertFalse(eligible)

    def test_static_and_durable_disabled_repositories_are_unioned_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(
                json.dumps(
                    {**valid_config(), "disabled_repositories": ["ANIKAWILLIAMS/STATIC"]}
                ),
                encoding="utf-8",
            )
            config = Config.load(path)
            StateStore(config.state_path).set_disabled_repositories(
                ["AnikaWilliams/Durable"]
            )

            controller = Controller(config, runner=RecordingRunner([]))  # type: ignore[arg-type]

            self.assertEqual(
                controller.list_disabled_repositories(),
                ["anikawilliams/durable", "anikawilliams/static"],
            )

    def test_each_run_reloads_durable_repository_switches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(
                json.dumps(
                    {**valid_config(), "disabled_repositories": ["ANIKAWILLIAMS/STATIC"]}
                ),
                encoding="utf-8",
            )
            config = Config.load(path)
            StateStore(config.state_path).set_disabled_repositories(
                ["AnikaWilliams/Before"]
            )
            controller = Controller(config, runner=RecordingRunner([]))  # type: ignore[arg-type]
            controller.store.set_disabled_repositories(["AnikaWilliams/After"])

            with patch.object(controller.github, "authored_open_prs", return_value=[]):
                controller.run(dry_run=False, verbose=False)

            self.assertEqual(
                controller.list_disabled_repositories(),
                ["anikawilliams/after", "anikawilliams/static"],
            )

    def test_forced_run_bypasses_unchanged_pr_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            summary = PRSummary(
                repository="AnikaWilliams/example",
                number=42,
                title="Example",
                url="https://github.com/AnikaWilliams/example/pull/42",
                updated_at="2026-08-10T00:00:00Z",
                is_draft=False,
            )
            controller.store.save(
                PRState(
                    repository=summary.repository,
                    number=summary.number,
                    updated_at=summary.updated_at,
                    head_sha=HEAD,
                    policy_revision=1,
                )
            )
            detail = {
                "headRefOid": HEAD,
                "headRefName": "feature/fix",
                "headRepository": {"nameWithOwner": summary.repository},
                "isDraft": False,
                "labels": {"nodes": [{"name": "hermes-pause"}]},
                "comments": {"nodes": []},
                "reviews": {"nodes": []},
            }

            with (
                patch.object(controller.github, "authored_open_prs", return_value=[summary]),
                patch.object(controller.github, "detail", return_value=detail) as get_detail,
            ):
                controller.run(dry_run=False, verbose=False)
                get_detail.assert_not_called()
                controller.run(dry_run=False, verbose=False, force=True)

            get_detail.assert_called_once_with(summary.repository, summary.number)

    def test_first_normal_tick_only_baselines_and_never_fetches_historical_details(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            runner = RecordingRunner(
                [
                    [
                        {
                            "repository": {"nameWithOwner": "AnikaWilliams/example"},
                            "number": 42,
                            "title": "Example",
                            "url": "https://github.com/AnikaWilliams/example/pull/42",
                            "updatedAt": "2026-08-10T00:00:00Z",
                            "isDraft": False,
                        }
                    ]
                ]
            )

            controller = Controller(Config.load(path), runner=runner)  # type: ignore[arg-type]
            events = controller.run(dry_run=False, verbose=False)

            self.assertEqual(events, [])
            self.assertEqual(len(runner.json_calls), 1)
            self.assertNotIn("graphql", runner.json_calls[0])
            state = controller.store.load("AnikaWilliams/example", 42)
            self.assertIsNotNone(state)
            self.assertEqual(state.head_sha if state else None, "")
            self.assertEqual(state.policy_revision if state else None, 0)

    def test_policy_revision_activates_an_unchanged_pr_with_one_fresh_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            summary_record = {
                "repository": {"nameWithOwner": "AnikaWilliams/example"},
                "number": 42,
                "title": "Example",
                "url": "https://github.com/AnikaWilliams/example/pull/42",
                "updatedAt": "2026-08-10T00:00:00Z",
                "isDraft": False,
            }
            detail = {
                "headRefOid": HEAD,
                "headRefName": "feature/fix",
                "headRepository": {"nameWithOwner": "AnikaWilliams/example"},
                "isDraft": False,
                "labels": {"nodes": []},
                "comments": {
                    "nodes": [
                        {
                            "databaseId": 987,
                            "author": {"login": "AnikaWilliams"},
                            "body": "Please review this pull request.",
                            "reactions": {"nodes": []},
                        }
                    ]
                },
                "reviews": {"nodes": []},
            }
            runner = RecordingRunner(
                [
                    [summary_record],
                    {"data": {"repository": {"pullRequest": detail}}},
                    {"id": 987},
                ]
            )
            controller = Controller(Config.load(path), runner=runner)  # type: ignore[arg-type]
            controller.store.save(
                PRState(
                    repository="AnikaWilliams/example",
                    number=42,
                    updated_at=summary_record["updatedAt"],
                    head_sha=HEAD,
                    policy_revision=0,
                )
            )

            events = controller.run(dry_run=False, verbose=False)

            state = controller.store.load("AnikaWilliams/example", 42)
            self.assertEqual(state.policy_revision if state else None, 1)
            self.assertEqual(state.requested_head if state else None, HEAD)
            self.assertEqual(state.requested_comment_id if state else None, "987")
            self.assertEqual(state.review_rounds if state else None, 1)
            review_calls = [
                call for call in runner.json_calls if any("/comments" in item for item in call)
            ]
            self.assertEqual(len(review_calls), 1)
            self.assertTrue(any("activated policy revision 1" in event.message for event in events))

            second_runner = RecordingRunner(
                [
                    [summary_record],
                    {"data": {"repository": {"pullRequest": detail}}},
                ]
            )
            second_controller = Controller(Config.load(path), runner=second_runner)  # type: ignore[arg-type]

            self.assertEqual(second_controller.run(dry_run=False, verbose=False), [])
            second_review_calls = [
                call
                for call in second_runner.json_calls
                if any("/comments" in item for item in call)
            ]
            self.assertEqual(second_review_calls, [])
            self.assertEqual(len(second_runner.json_calls), 2)

    def test_policy_activation_preserves_an_existing_active_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            summary_record = {
                "repository": {"nameWithOwner": "AnikaWilliams/example"},
                "number": 42,
                "title": "Example",
                "url": "https://github.com/AnikaWilliams/example/pull/42",
                "updatedAt": "2026-08-10T00:00:00Z",
                "isDraft": False,
            }
            detail = {
                "headRefOid": HEAD,
                "headRefName": "feature/fix",
                "headRepository": {"nameWithOwner": "AnikaWilliams/example"},
                "isDraft": False,
                "labels": {"nodes": []},
                "comments": {"nodes": []},
                "reviews": {"nodes": []},
            }
            runner = RecordingRunner(
                [
                    [summary_record],
                    {"data": {"repository": {"pullRequest": detail}}},
                    {"status": "done"},
                    {"status": "done"},
                    {"status": "running"},
                ]
            )
            controller = Controller(Config.load(path), runner=runner)  # type: ignore[arg-type]
            controller.store.save(
                PRState(
                    repository="AnikaWilliams/example",
                    number=42,
                    updated_at=summary_record["updatedAt"],
                    head_sha=HEAD,
                    active_task_id="t_verify",
                    active_task_status="scheduled",
                    pipeline_json='{"analyze":"t_analyze","fix":"t_fix","verify":"t_verify"}',
                    policy_revision=0,
                )
            )

            controller.run(dry_run=False, verbose=False)

            state = controller.store.load("AnikaWilliams/example", 42)
            self.assertEqual(state.policy_revision if state else None, 1)
            self.assertEqual(state.active_task_id if state else None, "t_verify")
            self.assertEqual(state.active_task_status if state else None, "running")
            self.assertFalse(any(any("/comments" in item for item in call) for call in runner.json_calls))

    def test_kanban_card_relies_on_profile_moa_without_a_task_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            runner = RecordingRunner([{"id": "t_example"}])
            client = KanbanClient(Config.load(path), runner)  # type: ignore[arg-type]

            task_id = client.create_fix_task(
                repository="AnikaWilliams/example",
                number=42,
                title="Example",
                body="A focused fix.",
                workspace=Path(temporary_directory) / "worktree",
                finding_fingerprint="fingerprint",
            )

            self.assertEqual(task_id, "t_example")
            command = runner.json_calls[0]
            self.assertIn("--assignee", command)
            self.assertEqual(command[command.index("--assignee") + 1], "prfix")
            self.assertIn("--skill", command)
            self.assertEqual(command[command.index("--skill") + 1], "pr-autopilot-fixer")
            self.assertEqual(command[command.index("--max-retries") + 1], "0")
            self.assertEqual(command[command.index("--max-turns") + 1], "40")
            self.assertNotIn("--model", command)
            self.assertNotIn("--provider", command)

    def test_merge_history_card_is_unassigned_terminal_and_contains_merge_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            runner = RecordingRunner([{"id": "t_merged"}])
            client = KanbanClient(Config.load(path), runner)  # type: ignore[arg-type]
            record = MergeHistoryRecord(
                repository="AnikaWilliams/example",
                number=42,
                title="Example change",
                url="https://github.com/AnikaWilliams/example/pull/42",
                head_sha=HEAD,
                merged_at="2026-08-10T20:00:00Z",
            )

            task_id = client.create_merge_history_card(record)
            client.complete_merge_history_card(record, task_id)

            self.assertEqual(task_id, "t_merged")
            create = runner.json_calls[0]
            self.assertEqual(create[:3], ["hermes", "kanban", "create"])
            self.assertTrue(create[3].startswith("[Merged] AnikaWilliams/example#42"))
            self.assertEqual(create[create.index("--initial-status") + 1], "running")
            self.assertEqual(
                create[create.index("--idempotency-key") + 1],
                f"pr-autopilot:AnikaWilliams/example:42:{HEAD}:merged",
            )
            self.assertEqual(
                create[create.index("--tenant") + 1],
                "pr-autopilot:AnikaWilliams/example#42",
            )
            for forbidden in (
                "--assignee",
                "--skill",
                "--model",
                "--provider",
                "--parent",
                "--workspace",
            ):
                self.assertNotIn(forbidden, create)

            complete = runner.run_calls[0]
            self.assertEqual(
                complete[:4], ["hermes", "kanban", "complete", "t_merged"]
            )
            metadata = json.loads(complete[complete.index("--metadata") + 1])
            self.assertEqual(metadata["repository"], "AnikaWilliams/example")
            self.assertEqual(metadata["pr_number"], 42)
            self.assertEqual(metadata["url"], record.url)
            self.assertEqual(metadata["head_sha"], HEAD)
            self.assertEqual(metadata["merge_method"], "squash")
            self.assertEqual(metadata["merged_at"], record.merged_at)

    def test_pending_merge_history_is_published_once_and_not_repeated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            runner = RecordingRunner([{"id": "t_merged"}])
            controller = Controller(Config.load(path), runner=runner)  # type: ignore[arg-type]
            controller.store.queue_merge_history(
                repository="AnikaWilliams/example",
                number=42,
                title="Example change",
                url="https://github.com/AnikaWilliams/example/pull/42",
                head_sha=HEAD,
                merged_at="2026-08-10T20:00:00Z",
            )

            events = controller._publish_pending_merge_history(dry_run=False)

            self.assertEqual(len(events), 1)
            self.assertIn("recorded merged history card t_merged", events[0].message)
            self.assertEqual(controller.store.pending_merge_history(), [])
            saved = controller.store.load_merge_history("AnikaWilliams/example", 42)
            self.assertEqual(saved.task_id if saved else None, "t_merged")
            self.assertIsNotNone(saved.recorded_at if saved else None)
            calls_after_first_publication = len(runner.json_calls) + len(runner.run_calls)

            self.assertEqual(
                controller._publish_pending_merge_history(dry_run=False), []
            )
            self.assertEqual(
                len(runner.json_calls) + len(runner.run_calls),
                calls_after_first_publication,
            )

    def test_unconfirmed_merge_intent_recovers_only_for_exact_merged_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            controller.store.queue_merge_history_intent(
                repository="AnikaWilliams/example",
                number=42,
                title="Example change",
                url="https://github.com/AnikaWilliams/example/pull/42",
                head_sha=HEAD,
                intended_at="2026-08-10T19:59:59Z",
            )

            with patch.object(
                controller.github,
                "merge_status",
                return_value={
                    "state": "MERGED",
                    "headRefOid": HEAD,
                    "mergedAt": "2026-08-10T20:00:00Z",
                },
            ):
                events = controller._reconcile_unconfirmed_merge_history()

            history = controller.store.load_merge_history("AnikaWilliams/example", 42)
            self.assertEqual(history.confirmed_at if history else None, "2026-08-10T20:00:00Z")
            self.assertEqual(controller.store.unconfirmed_merge_history(), [])
            self.assertEqual(controller.store.pending_merge_history(), [history])
            self.assertTrue(any("recovered merged history intent" in event.message for event in events))

    def test_open_pr_that_moved_to_a_new_head_invalidates_old_merge_intent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            controller.store.queue_merge_history_intent(
                repository="AnikaWilliams/example",
                number=42,
                title="Example change",
                url="https://github.com/AnikaWilliams/example/pull/42",
                head_sha=HEAD,
                intended_at="2026-08-10T19:59:59Z",
            )
            new_head = "f" * 40

            with patch.object(
                controller.github,
                "merge_status",
                return_value={"state": "OPEN", "headRefOid": new_head, "mergedAt": None},
            ):
                events = controller._reconcile_unconfirmed_merge_history()

            history = controller.store.load_merge_history(
                "AnikaWilliams/example", 42, head_sha=HEAD
            )
            self.assertIsNotNone(history)
            self.assertIsNotNone(history.invalidated_at if history else None)
            self.assertIn("moved to a different head", history.invalidation_reason if history else "")
            self.assertEqual(controller.store.unconfirmed_merge_history(), [])
            self.assertTrue(any("moved to a different head" in event.message for event in events))

    def test_closed_unmerged_pr_invalidates_an_unchanged_head_merge_intent(self) -> None:
        """A closed PR cannot leave an exact-head merge intent eligible to recover."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            controller.store.queue_merge_history_intent(
                repository="AnikaWilliams/example",
                number=42,
                title="Example change",
                url="https://github.com/AnikaWilliams/example/pull/42",
                head_sha=HEAD,
                intended_at="2026-08-10T19:59:59Z",
            )

            with patch.object(
                controller.github,
                "merge_status",
                return_value={"state": "CLOSED", "headRefOid": HEAD, "mergedAt": None},
            ):
                events = controller._reconcile_unconfirmed_merge_history()

            history = controller.store.load_merge_history("AnikaWilliams/example", 42)
            self.assertIsNotNone(history)
            self.assertIsNotNone(history.invalidated_at if history else None)
            self.assertIn("closed", history.invalidation_reason if history else "")
            self.assertEqual(controller.store.unconfirmed_merge_history(), [])
            self.assertEqual(controller.store.pending_merge_history(), [])
            self.assertTrue(any("closed" in event.message for event in events))

    def test_reopened_same_head_after_closed_merge_intent_requests_a_fresh_review(self) -> None:
        """An old clean reaction must not reauthorize a PR that GitHub closed."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            summary = PRSummary(
                repository="AnikaWilliams/example",
                number=42,
                title="Reopened example change",
                url="https://github.com/AnikaWilliams/example/pull/42",
                updated_at="2026-08-10T20:01:00Z",
                is_draft=False,
            )
            old_request_id = "old-clean-review-request"
            controller.store.save(
                PRState(
                    repository=summary.repository,
                    number=summary.number,
                    updated_at="2026-08-10T20:00:00Z",
                    head_sha=HEAD,
                    policy_revision=1,
                    requested_head=HEAD,
                    requested_comment_id=old_request_id,
                    requested_at="2026-08-10T19:59:59Z",
                    review_rounds=1,
                )
            )
            controller.store.queue_merge_history_intent(
                repository=summary.repository,
                number=summary.number,
                title=summary.title,
                url=summary.url,
                head_sha=HEAD,
                intended_at="2026-08-10T20:00:00Z",
                authorization_id=old_request_id,
            )
            with patch.object(
                controller.github,
                "merge_status",
                return_value={"state": "CLOSED", "headRefOid": HEAD, "mergedAt": None},
            ):
                controller._reconcile_unconfirmed_merge_history()

            detail = {
                "state": "OPEN",
                "headRefOid": HEAD,
                "headRefName": "feature/reopened",
                "headRepository": {"nameWithOwner": summary.repository},
                "mergeStateStatus": "CLEAN",
                "labels": {"nodes": []},
                "comments": {
                    "nodes": [
                        {
                            "id": old_request_id,
                            "author": {"login": "AnikaWilliams"},
                            "body": "@codex review",
                            "reactions": {"nodes": []},
                        },
                        {
                            "author": {"login": "chatgpt-codex-connector"},
                            "body": (
                                "Codex Review: Didn't find any major issues.\n"
                                f"**Reviewed commit:** `{HEAD[:10]}`"
                            ),
                            "reactions": {"nodes": []},
                        },
                    ]
                },
                "reviews": {"nodes": []},
            }

            with (
                patch.object(controller.github, "authored_open_prs", return_value=[summary]),
                patch.object(controller.github, "detail", return_value=detail),
                patch.object(controller, "_eligible", return_value=True),
                patch.object(controller, "_source_repository", return_value=summary.repository),
                patch.object(
                    controller.github,
                    "request_codex_review",
                    return_value="fresh-review-request",
                ) as request_review,
                patch.object(controller.github, "merge_squash") as merge_squash,
                patch.object(controller, "_publish_pending_merge_history", return_value=[]),
            ):
                first_events = controller.run(dry_run=False, verbose=False)
                after_rejection = controller.store.load(summary.repository, summary.number)
                self.assertIsNone(
                    after_rejection.requested_head if after_rejection else "missing"
                )
                self.assertIsNone(
                    after_rejection.requested_comment_id if after_rejection else "missing"
                )
                self.assertTrue(
                    after_rejection.fresh_review_required if after_rejection else False
                )
                second_events = controller.run(dry_run=False, verbose=False)

            request_review.assert_called_once_with(summary.repository, summary.number)
            merge_squash.assert_not_called()
            state = controller.store.load(summary.repository, summary.number)
            self.assertEqual(state.requested_head if state else None, HEAD)
            self.assertEqual(
                state.requested_comment_id if state else None, "fresh-review-request"
            )
            self.assertEqual(state.review_rounds if state else None, 1)
            self.assertFalse(state.fresh_review_required if state else True)
            self.assertEqual(controller.store.unconfirmed_merge_history(), [])
            self.assertTrue(
                any("authorization" in event.message for event in first_events)
            )
            self.assertTrue(
                any("requested Codex review" in event.message for event in second_events)
            )

    def test_missing_tracked_request_replaces_review_without_using_history(self) -> None:
        """A deleted request must not leave an unchanged head in REVIEWING forever."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            summary = PRSummary(
                repository="AnikaWilliams/example",
                number=42,
                title="Example change",
                url="https://github.com/AnikaWilliams/example/pull/42",
                updated_at="2026-08-10T20:01:00Z",
                is_draft=False,
            )
            controller.store.save(
                PRState(
                    repository=summary.repository,
                    number=summary.number,
                    updated_at=summary.updated_at,
                    head_sha=HEAD,
                    policy_revision=1,
                    requested_head=HEAD,
                    requested_comment_id="deleted-request",
                    requested_at="2026-08-10T20:00:00Z",
                    review_rounds=1,
                )
            )
            detail = {
                "headRefOid": HEAD,
                "headRefName": "feature/example",
                "headRepository": {"nameWithOwner": summary.repository},
                "isDraft": False,
                "mergeStateStatus": "CLEAN",
                "labels": {"nodes": []},
                "comments": {
                    "nodes": [
                        {
                            "author": {"login": "chatgpt-codex-connector[bot]"},
                            "body": (
                                "Codex Review: Didn't find any major issues.\n"
                                f"**Reviewed commit:** `{HEAD[:10]}`"
                            ),
                            "reactions": {"nodes": []},
                        }
                    ]
                },
                "reviews": {
                    "nodes": [
                        {
                            "author": {"login": "chatgpt-codex-connector[bot]"},
                            "body": (
                                "**<sub><sub>![P1 Badge](badge.svg)</sub></sub> "
                                "Historical finding.\n\n"
                                f"**Reviewed commit:** `{HEAD[:10]}`"
                            ),
                            "commit": {"oid": HEAD},
                            "state": "COMMENTED",
                            "comments": {"nodes": []},
                        }
                    ]
                },
            }

            with (
                patch.object(controller.github, "authored_open_prs", return_value=[summary]),
                patch.object(controller.github, "detail", return_value=detail) as get_detail,
                patch.object(controller, "_eligible", return_value=True),
                patch.object(controller, "_source_repository", return_value=summary.repository),
                patch.object(
                    controller.github,
                    "request_codex_review",
                    return_value="replacement-request",
                ) as request_review,
                patch.object(controller.github, "merge_squash") as merge_squash,
            ):
                events = controller.run(dry_run=False, verbose=False)

            get_detail.assert_called_once_with(
                summary.repository, summary.number, requested_comment_id="deleted-request"
            )
            request_review.assert_called_once_with(summary.repository, summary.number)
            merge_squash.assert_not_called()
            state = controller.store.load(summary.repository, summary.number)
            self.assertEqual(state.requested_head if state else None, HEAD)
            self.assertEqual(
                state.requested_comment_id if state else None, "replacement-request"
            )
            self.assertEqual(state.review_rounds if state else None, 2)
            self.assertTrue(any("requested Codex review" in event.message for event in events))

    def test_dry_run_does_not_confirm_or_invalidate_merge_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            for repository, number in (
                ("AnikaWilliams/merged", 41),
                ("AnikaWilliams/moved", 42),
            ):
                controller.store.queue_merge_history_intent(
                    repository=repository,
                    number=number,
                    title="Example change",
                    url=f"https://github.com/{repository}/pull/{number}",
                    head_sha=HEAD,
                    intended_at="2026-08-10T19:59:59Z",
                )

            with (
                patch.object(
                    controller.github,
                    "merge_status",
                    side_effect=[
                        {
                            "state": "MERGED",
                            "headRefOid": HEAD,
                            "mergedAt": "2026-08-10T20:00:00Z",
                        },
                        {"state": "OPEN", "headRefOid": "f" * 40, "mergedAt": None},
                    ],
                ),
                patch.object(controller.github, "authored_open_prs", return_value=[]),
            ):
                events = controller.run(dry_run=True, verbose=True)

            records = controller.store.unconfirmed_merge_history()
            self.assertEqual(
                {(record.repository, record.number) for record in records},
                {("AnikaWilliams/merged", 41), ("AnikaWilliams/moved", 42)},
            )
            self.assertTrue(any("would recover merged history intent" in event.message for event in events))
            self.assertTrue(any("would invalidate merge history intent" in event.message for event in events))

    def test_different_merged_head_invalidates_intent_once_without_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            controller.store.queue_merge_history_intent(
                repository="AnikaWilliams/example",
                number=42,
                title="Example change",
                url="https://github.com/AnikaWilliams/example/pull/42",
                head_sha=HEAD,
                intended_at="2026-08-10T19:59:59Z",
            )
            different_head = "f" * 40

            with patch.object(
                controller.github,
                "merge_status",
                return_value={
                    "state": "MERGED",
                    "headRefOid": different_head,
                    "mergedAt": "2026-08-10T20:00:00Z",
                },
            ) as merge_status:
                first_events = controller._reconcile_unconfirmed_merge_history()
                second_events = controller._reconcile_unconfirmed_merge_history()

            history = controller.store.load_merge_history("AnikaWilliams/example", 42)
            self.assertIsNotNone(history)
            self.assertIsNone(history.confirmed_at if history else None)
            self.assertIsNone(history.recorded_at if history else None)
            self.assertIsNotNone(history.invalidated_at if history else None)
            self.assertIn("different head", history.invalidation_reason if history else "")
            self.assertEqual(controller.store.unconfirmed_merge_history(), [])
            self.assertEqual(controller.store.pending_merge_history(), [])
            self.assertEqual(len(first_events), 1)
            self.assertIn("blocked merge history intent", first_events[0].message)
            self.assertEqual(second_events, [])
            merge_status.assert_called_once_with("AnikaWilliams/example", 42)

    def test_controller_tick_flushes_pending_merge_history_before_open_pr_poll(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            runner = RecordingRunner([{"id": "t_merged"}, []])
            controller = Controller(Config.load(path), runner=runner)  # type: ignore[arg-type]
            controller.store.queue_merge_history(
                repository="AnikaWilliams/example",
                number=42,
                title="Example change",
                url="https://github.com/AnikaWilliams/example/pull/42",
                head_sha=HEAD,
                merged_at="2026-08-10T20:00:00Z",
            )

            events = controller.run(dry_run=False, verbose=False)

            self.assertEqual(len(events), 1)
            self.assertIn("recorded merged history card t_merged", events[0].message)
            self.assertEqual(
                runner.json_calls[0][:3], ["hermes", "kanban", "create"]
            )
            self.assertEqual(runner.json_calls[1][:3], ["gh", "search", "prs"])
            self.assertEqual(controller.store.pending_merge_history(), [])

    def test_successful_guarded_merge_creates_one_completed_history_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            summary = {
                "repository": {"nameWithOwner": "AnikaWilliams/example"},
                "number": 42,
                "title": "Example change",
                "url": "https://github.com/AnikaWilliams/example/pull/42",
                "updatedAt": "2026-08-10T20:00:00Z",
                "isDraft": False,
            }
            detail = {
                "headRefOid": HEAD,
                "headRefName": "feature/fix",
                "headRepository": {"nameWithOwner": "AnikaWilliams/example"},
                "isDraft": False,
                "mergeStateStatus": "CLEAN",
                "labels": {"nodes": []},
                "comments": {
                    "nodes": [
                        {
                            "author": {"login": "chatgpt-codex-connector"},
                            "body": (
                                "Codex Review: Didn't find any major issues.\n"
                                f"**Reviewed commit:** `{HEAD[:10]}`"
                            ),
                        }
                    ]
                },
                "reviews": {"nodes": []},
            }
            runner = RecordingRunner(
                [
                    [summary],
                    {"data": {"repository": {"pullRequest": detail}}},
                    {"id": "t_merged"},
                ]
            )
            controller = Controller(Config.load(path), runner=runner)  # type: ignore[arg-type]
            controller.store.save(
                PRState(
                    repository="AnikaWilliams/example",
                    number=42,
                    updated_at="2026-08-10T19:55:00Z",
                    head_sha=HEAD,
                    policy_revision=1,
                    requested_head=HEAD,
                )
            )
            intents_seen_during_merge = []

            def observe_persisted_intent(*_args: object) -> None:
                intents_seen_during_merge.extend(
                    controller.store.unconfirmed_merge_history()
                )

            with (
                patch.object(
                    controller.github,
                    "merge_squash",
                    side_effect=observe_persisted_intent,
                ),
                patch.object(
                    controller.github,
                    "merge_status",
                    return_value=exact_merged_status(),
                ),
            ):
                events = controller.run(dry_run=False, verbose=False)

            history = controller.store.load_merge_history(
                "AnikaWilliams/example", 42
            )
            self.assertIsNotNone(history)
            self.assertEqual(history.task_id if history else None, "t_merged")
            self.assertIsNotNone(history.recorded_at if history else None)
            self.assertEqual(controller.store.pending_merge_history(), [])
            self.assertEqual(len(intents_seen_during_merge), 1)
            self.assertEqual(intents_seen_during_merge[0].head_sha, HEAD)
            self.assertIsNone(intents_seen_during_merge[0].confirmed_at)
            self.assertEqual(
                len(
                    [
                        call
                        for call in runner.json_calls
                        if call[:3] == ["hermes", "kanban", "create"]
                    ]
                ),
                1,
            )

            self.assertTrue(
                any(
                    call[:3] == ["hermes", "kanban", "complete"]
                    for call in runner.run_calls
                )
            )
            self.assertTrue(
                any("recorded merged history card t_merged" in event.message for event in events)
            )

    def test_merge_queue_submission_stays_unconfirmed_until_github_reports_merged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            summary = PRSummary(
                repository="AnikaWilliams/example",
                number=42,
                title="Example change",
                url="https://github.com/AnikaWilliams/example/pull/42",
                updated_at="2026-08-10T20:00:00Z",
                is_draft=False,
            )
            detail = {
                "headRefOid": HEAD,
                "headRefName": "feature/fix",
                "headRepository": {"nameWithOwner": summary.repository},
                "mergeStateStatus": "CLEAN",
                "labels": {"nodes": []},
                "comments": {
                    "nodes": [
                        {
                            "author": {"login": "chatgpt-codex-connector"},
                            "body": (
                                "Codex Review: Didn't find any major issues.\n"
                                f"**Reviewed commit:** `{HEAD[:10]}`"
                            ),
                        }
                    ]
                },
                "reviews": {"nodes": []},
            }
            controller.store.save(
                PRState(
                    repository=summary.repository,
                    number=summary.number,
                    updated_at="2026-08-10T19:55:00Z",
                    head_sha=HEAD,
                    policy_revision=1,
                    requested_head=HEAD,
                )
            )

            with (
                patch.object(controller.github, "authored_open_prs", return_value=[summary]),
                patch.object(controller.github, "detail", return_value=detail),
                patch.object(controller, "_eligible", return_value=True),
                patch.object(controller, "_source_repository", return_value=summary.repository),
                patch.object(controller.github, "merge_squash") as merge_squash,
                patch.object(
                    controller.github,
                    "merge_status",
                    return_value={
                        "state": "OPEN",
                        "headRefOid": HEAD,
                        "mergedAt": None,
                    },
                ),
                patch.object(controller, "_publish_pending_merge_history", return_value=[]),
            ):
                events = controller.run(dry_run=False, verbose=False)

            history = controller.store.load_merge_history(
                summary.repository, summary.number, head_sha=HEAD
            )
            merge_squash.assert_called_once_with(summary.repository, summary.number, HEAD)
            self.assertIsNotNone(history)
            self.assertIsNone(history.confirmed_at if history else "missing")
            self.assertEqual(controller.store.pending_merge_history(), [])
            self.assertTrue(
                any(
                    "waiting for GitHub exact-head confirmation (OPEN)" in event.message
                    for event in events
                )
            )

    def test_failed_guarded_merge_leaves_intent_and_continues_to_later_prs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            summary = PRSummary(
                repository="AnikaWilliams/example",
                number=42,
                title="Example change",
                url="https://github.com/AnikaWilliams/example/pull/42",
                updated_at="2026-08-10T20:00:00Z",
                is_draft=False,
            )
            detail = {
                "headRefOid": HEAD,
                "headRefName": "feature/fix",
                "headRepository": {"nameWithOwner": "AnikaWilliams/example"},
                "mergeStateStatus": "CLEAN",
                "labels": {"nodes": []},
                "comments": {
                    "nodes": [
                        {
                            "author": {"login": "chatgpt-codex-connector"},
                            "body": (
                                "Codex Review: Didn't find any major issues.\n"
                                f"**Reviewed commit:** `{HEAD[:10]}`"
                            ),
                        }
                    ]
                },
                "reviews": {"nodes": []},
            }
            controller.store.save(
                PRState(
                    repository=summary.repository,
                    number=summary.number,
                    updated_at="2026-08-10T19:55:00Z",
                    head_sha=HEAD,
                    policy_revision=1,
                    requested_head=HEAD,
                )
            )
            later_summary = PRSummary(
                repository="AnikaWilliams/later",
                number=43,
                title="Later change",
                url="https://github.com/AnikaWilliams/later/pull/43",
                updated_at="2026-08-10T20:01:00Z",
                is_draft=False,
            )
            controller.store.save(
                PRState(
                    repository=later_summary.repository,
                    number=later_summary.number,
                    updated_at="2026-08-10T19:55:00Z",
                    head_sha=HEAD,
                    policy_revision=1,
                )
            )

            with (
                patch.object(
                    controller.github,
                    "authored_open_prs",
                    return_value=[summary, later_summary],
                ),
                patch.object(controller.github, "detail", side_effect=[detail, {}]),
                patch.object(controller, "_eligible", return_value=True),
                patch.object(controller, "_source_repository", return_value=summary.repository),
                patch.object(
                    controller.github,
                    "merge_squash",
                    side_effect=CommandError("merge rejected"),
                ),
            ):
                events = controller.run(dry_run=False, verbose=False)

            intents = controller.store.unconfirmed_merge_history()
            self.assertEqual(len(intents), 1)
            self.assertEqual(intents[0].head_sha, HEAD)
            self.assertEqual(controller.store.pending_merge_history(), [])
            self.assertTrue(any("merge pending: merge rejected" in event.message for event in events))
            self.assertTrue(
                any(
                    event.repository == later_summary.repository
                    and "GitHub did not provide a head SHA" in event.message
                    for event in events
                )
            )

    def test_failed_guarded_merge_retries_on_the_next_unchanged_tick(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            summary = PRSummary(
                repository="AnikaWilliams/example",
                number=42,
                title="Example change",
                url="https://github.com/AnikaWilliams/example/pull/42",
                updated_at="2026-08-10T20:00:00Z",
                is_draft=False,
            )
            detail = {
                "headRefOid": HEAD,
                "headRefName": "feature/fix",
                "headRepository": {"nameWithOwner": summary.repository},
                "mergeStateStatus": "CLEAN",
                "labels": {"nodes": []},
                "comments": {
                    "nodes": [
                        {
                            "author": {"login": "chatgpt-codex-connector"},
                            "body": (
                                "Codex Review: Didn't find any major issues.\n"
                                f"**Reviewed commit:** `{HEAD[:10]}`"
                            ),
                        }
                    ]
                },
                "reviews": {"nodes": []},
            }
            controller.store.save(
                PRState(
                    repository=summary.repository,
                    number=summary.number,
                    updated_at="2026-08-10T19:55:00Z",
                    head_sha=HEAD,
                    policy_revision=1,
                    requested_head=HEAD,
                )
            )

            with (
                patch.object(controller.github, "authored_open_prs", return_value=[summary]),
                patch.object(controller.github, "detail", return_value=detail),
                patch.object(
                    controller.github,
                    "merge_status",
                    side_effect=[
                        {"state": "OPEN", "headRefOid": HEAD, "mergedAt": None},
                        exact_merged_status(),
                    ],
                ),
                patch.object(controller, "_eligible", return_value=True),
                patch.object(controller, "_source_repository", return_value=summary.repository),
                patch.object(
                    controller.github,
                    "merge_squash",
                    side_effect=[CommandError("merge rejected"), None],
                ) as merge_squash,
                patch.object(controller, "_publish_pending_merge_history", return_value=[]),
            ):
                first_events = controller.run(dry_run=False, verbose=False)
                second_events = controller.run(dry_run=False, verbose=False)

            history = controller.store.load_merge_history(summary.repository, summary.number)
            self.assertEqual(merge_squash.call_count, 2)
            self.assertTrue(any("merge pending: merge rejected" in event.message for event in first_events))
            self.assertTrue(any("squash-merged exact clean head" in event.message for event in second_events))
            self.assertIsNotNone(history.confirmed_at if history else None)

    def test_non_clean_merge_state_is_rechecked_on_the_next_unchanged_tick(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            summary = PRSummary(
                repository="AnikaWilliams/example",
                number=42,
                title="Example change",
                url="https://github.com/AnikaWilliams/example/pull/42",
                updated_at="2026-08-10T20:00:00Z",
                is_draft=False,
            )
            detail = {
                "headRefOid": HEAD,
                "headRefName": "feature/fix",
                "headRepository": {"nameWithOwner": summary.repository},
                "labels": {"nodes": []},
                "comments": {
                    "nodes": [
                        {
                            "author": {"login": "chatgpt-codex-connector"},
                            "body": (
                                "Codex Review: Didn't find any major issues.\n"
                                f"**Reviewed commit:** `{HEAD[:10]}`"
                            ),
                        }
                    ]
                },
                "reviews": {"nodes": []},
            }
            controller.store.save(
                PRState(
                    repository=summary.repository,
                    number=summary.number,
                    updated_at="2026-08-10T19:55:00Z",
                    head_sha=HEAD,
                    policy_revision=1,
                    requested_head=HEAD,
                    active_task_id="t_verify",
                    active_task_status="scheduled",
                    pipeline_json='{"analyze":"t_analyze","fix":"t_fix","verify":"t_verify"}',
                )
            )

            with (
                patch.object(controller.github, "authored_open_prs", return_value=[summary]),
                patch.object(
                    controller.github,
                    "detail",
                    side_effect=[
                        {**detail, "mergeStateStatus": "BLOCKED"},
                        {**detail, "mergeStateStatus": "CLEAN"},
                    ],
                ) as detail_call,
                patch.object(controller.kanban, "status", return_value="done"),
                patch.object(controller, "_eligible", return_value=True),
                patch.object(controller, "_source_repository", return_value=summary.repository),
                patch.object(controller.github, "merge_squash") as merge_squash,
                patch.object(
                    controller.github,
                    "merge_status",
                    return_value=exact_merged_status(),
                ),
                patch.object(controller, "_publish_pending_merge_history", return_value=[]),
            ):
                first_events = controller.run(dry_run=False, verbose=False)
                second_events = controller.run(dry_run=False, verbose=False)

            self.assertEqual(detail_call.call_count, 2)
            merge_squash.assert_called_once_with(summary.repository, summary.number, HEAD)
            self.assertTrue(any("waiting for GitHub merge state BLOCKED" in event.message for event in first_events))
            self.assertTrue(any("squash-merged exact clean head" in event.message for event in second_events))

    def test_pending_exact_head_review_or_archived_pipeline_bypasses_unchanged_shortcut(self) -> None:
        for terminal_status in (None, "archived"):
            with self.subTest(terminal_status=terminal_status):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    path = Path(temporary_directory) / "config.json"
                    path.write_text(json.dumps(valid_config()), encoding="utf-8")
                    controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
                    summary = PRSummary(
                        repository="AnikaWilliams/example",
                        number=42,
                        title="Example change",
                        url="https://github.com/AnikaWilliams/example/pull/42",
                        updated_at="2026-08-10T20:00:00Z",
                        is_draft=False,
                    )
                    detail = {
                        "headRefOid": HEAD,
                        "headRefName": "feature/fix",
                        "headRepository": {"nameWithOwner": summary.repository},
                        "mergeStateStatus": "CLEAN",
                        "labels": {"nodes": []},
                        "comments": {
                            "nodes": [
                                {
                                    "author": {"login": "chatgpt-codex-connector"},
                                    "body": (
                                        "Codex Review: Didn't find any major issues.\n"
                                        f"**Reviewed commit:** `{HEAD[:10]}`"
                                    ),
                                }
                            ]
                        },
                        "reviews": {"nodes": []},
                    }
                    has_pipeline = terminal_status is not None
                    controller.store.save(
                        PRState(
                            repository=summary.repository,
                            number=summary.number,
                            updated_at=summary.updated_at,
                            head_sha=HEAD,
                            policy_revision=1,
                            requested_head=HEAD,
                            active_task_id="t_verify" if has_pipeline else None,
                            active_task_status=terminal_status,
                            pipeline_json=(
                                '{"analyze":"t_analyze","fix":"t_fix","verify":"t_verify"}'
                                if has_pipeline
                                else None
                            ),
                        )
                    )

                    with (
                        patch.object(controller.github, "authored_open_prs", return_value=[summary]),
                        patch.object(controller.github, "detail", return_value=detail) as detail_call,
                        patch.object(controller.kanban, "status", return_value="archived"),
                        patch.object(controller, "_eligible", return_value=True),
                        patch.object(controller, "_source_repository", return_value=summary.repository),
                        patch.object(controller.github, "merge_squash") as merge_squash,
                        patch.object(
                            controller.github,
                            "merge_status",
                            return_value=exact_merged_status(),
                        ),
                        patch.object(controller, "_publish_pending_merge_history", return_value=[]),
                    ):
                        events = controller.run(dry_run=False, verbose=False)

                    detail_call.assert_called_once_with(summary.repository, summary.number)
                    merge_squash.assert_called_once_with(summary.repository, summary.number, HEAD)
                    self.assertTrue(any("squash-merged exact clean head" in event.message for event in events))

    def test_unconfirmed_exact_head_intent_bypasses_the_unchanged_shortcut(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            controller = Controller(Config.load(path), runner=RecordingRunner([]))  # type: ignore[arg-type]
            summary = PRSummary(
                repository="AnikaWilliams/example",
                number=42,
                title="Example change",
                url="https://github.com/AnikaWilliams/example/pull/42",
                updated_at="2026-08-10T20:00:00Z",
                is_draft=False,
            )
            detail = {
                "headRefOid": HEAD,
                "headRefName": "feature/fix",
                "headRepository": {"nameWithOwner": summary.repository},
                "mergeStateStatus": "CLEAN",
                "labels": {"nodes": []},
                "comments": {
                    "nodes": [
                        {
                            "author": {"login": "chatgpt-codex-connector"},
                            "body": (
                                "Codex Review: Didn't find any major issues.\n"
                                f"**Reviewed commit:** `{HEAD[:10]}`"
                            ),
                        }
                    ]
                },
                "reviews": {"nodes": []},
            }
            controller.store.save(
                PRState(
                    repository=summary.repository,
                    number=summary.number,
                    updated_at=summary.updated_at,
                    head_sha=HEAD,
                    policy_revision=1,
                    requested_head=HEAD,
                )
            )
            controller.store.queue_merge_history_intent(
                repository=summary.repository,
                number=summary.number,
                title=summary.title,
                url=summary.url,
                head_sha=HEAD,
                intended_at="2026-08-10T19:59:59Z",
            )

            with (
                patch.object(controller.github, "authored_open_prs", return_value=[summary]),
                patch.object(controller.github, "detail", return_value=detail) as detail_call,
                patch.object(
                    controller.github,
                    "merge_status",
                    side_effect=[
                        {"state": "OPEN", "headRefOid": HEAD, "mergedAt": None},
                        exact_merged_status(),
                    ],
                ),
                patch.object(controller, "_eligible", return_value=True),
                patch.object(controller, "_source_repository", return_value=summary.repository),
                patch.object(controller.github, "merge_squash") as merge_squash,
                patch.object(controller, "_publish_pending_merge_history", return_value=[]),
            ):
                events = controller.run(dry_run=False, verbose=False)

            history = controller.store.load_merge_history(summary.repository, summary.number)
            detail_call.assert_called_once_with(summary.repository, summary.number)
            merge_squash.assert_called_once_with(summary.repository, summary.number, HEAD)
            self.assertTrue(any("squash-merged exact clean head" in event.message for event in events))
            self.assertIsNotNone(history.confirmed_at if history else None)

    def test_failed_history_completion_retries_the_same_task_without_stopping_polling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            failing_runner = FailingCompletionRunner([{"id": "t_merged"}, []])
            controller = Controller(Config.load(path), runner=failing_runner)  # type: ignore[arg-type]
            controller.store.queue_merge_history(
                repository="AnikaWilliams/example",
                number=42,
                title="Example change",
                url="https://github.com/AnikaWilliams/example/pull/42",
                head_sha=HEAD,
                merged_at="2026-08-10T20:00:00Z",
            )

            failed_events = controller.run(dry_run=False, verbose=False)

            pending = controller.store.pending_merge_history()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].task_id, "t_merged")
            self.assertTrue(
                any("merged history pending" in event.message for event in failed_events)
            )
            self.assertEqual(
                failing_runner.json_calls[-1][:3], ["gh", "search", "prs"]
            )

            retry_runner = RecordingRunner([{"status": "running"}, []])
            retry = Controller(Config.load(path), runner=retry_runner)  # type: ignore[arg-type]
            retry_events = retry.run(dry_run=False, verbose=False)

            self.assertEqual(retry.store.pending_merge_history(), [])
            self.assertFalse(
                any(
                    call[:3] == ["hermes", "kanban", "create"]
                    for call in retry_runner.json_calls
                )
            )
            self.assertTrue(
                any(
                    call[:4] == ["hermes", "kanban", "complete", "t_merged"]
                    for call in retry_runner.run_calls
                )
            )
            self.assertTrue(
                any(
                    "recorded merged history card t_merged" in event.message
                    for event in retry_events
                )
            )

    def test_kanban_pipeline_is_three_linked_specialist_cards_without_model_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            runner = RecordingRunner(
                [{"id": "t_analyze"}, {"id": "t_fix"}, {"id": "t_verify"}]
            )
            client = KanbanClient(Config.load(path), runner)  # type: ignore[arg-type]

            tasks = client.create_pipeline(
                repository="AnikaWilliams/example",
                number=42,
                workspace=Path(temporary_directory) / "worktree",
                finding_fingerprint="fingerprint",
                analysis_body="Analyze this finding.",
                fix_body="Fix this finding.",
                verification_body="Verify this fix.",
            )

            self.assertEqual(
                tasks,
                {"analyze": "t_analyze", "fix": "t_fix", "verify": "t_verify"},
            )
            analyze, fix, verify = runner.json_calls
            for command, profile, skill in (
                (analyze, "prtriage", "pr-autopilot-analyzer"),
                (fix, "prfix", "pr-autopilot-fixer"),
                (verify, "prverify", "pr-autopilot-verifier"),
            ):
                self.assertEqual(command[:3], ["hermes", "kanban", "create"])
                self.assertEqual(command[command.index("--assignee") + 1], profile)
                self.assertEqual(command[command.index("--skill") + 1], skill)
                self.assertEqual(
                    command[command.index("--tenant") + 1],
                    "pr-autopilot:AnikaWilliams/example#42",
                )
                self.assertNotIn("--model", command)
                self.assertNotIn("--provider", command)

            self.assertEqual(fix[fix.index("--parent") + 1], "t_analyze")
            self.assertEqual(verify[verify.index("--parent") + 1], "t_fix")
            self.assertIn("analyze", analyze[analyze.index("--idempotency-key") + 1])
            self.assertIn("fix", fix[fix.index("--idempotency-key") + 1])
            self.assertIn("verify", verify[verify.index("--idempotency-key") + 1])
            self.assertEqual(
                analyze[analyze.index("--max-runtime") + 1],
                "90m",
            )
            self.assertEqual(
                fix[fix.index("--max-runtime") + 1],
                "180m",
            )
            self.assertEqual(
                verify[verify.index("--max-runtime") + 1],
                "60m",
            )
            self.assertEqual(
                analyze[analyze.index("--max-turns") + 1],
                "18",
            )
            self.assertEqual(
                fix[fix.index("--max-turns") + 1],
                "40",
            )
            self.assertEqual(
                verify[verify.index("--max-turns") + 1],
                "20",
            )

    def test_kanban_recognizes_ready_and_scheduled_pipeline_statuses(self) -> None:
        self.assertEqual(KanbanClient._task_status({"status": "ready"}), "ready")
        self.assertEqual(KanbanClient._task_status({"status": "scheduled"}), "scheduled")

    def test_fresh_findings_create_and_track_the_three_stage_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            detail = {
                "headRefOid": HEAD,
                "headRefName": "feature/fix",
                "isDraft": False,
                "mergeStateStatus": "UNKNOWN",
                "headRepository": {"nameWithOwner": "AnikaWilliams/example"},
                "labels": {"nodes": []},
                "comments": {"nodes": []},
                "reviews": {
                    "nodes": [
                        {
                            "author": {"login": "chatgpt-codex-connector"},
                            "body": f"**Reviewed commit:** `{HEAD[:10]}`",
                            "comments": {
                                "nodes": [
                                    {
                                        "databaseId": 99,
                                        "body": "Guard the edge case.",
                                        "path": "src/example.py",
                                        "line": 12,
                                    }
                                ]
                            },
                        }
                    ]
                },
            }
            runner = WorkspaceRecordingRunner(
                [
                    [
                        {
                            "repository": {"nameWithOwner": "AnikaWilliams/example"},
                            "number": 42,
                            "title": "Example",
                            "url": "https://github.com/AnikaWilliams/example/pull/42",
                            "updatedAt": "2026-08-10T01:00:00Z",
                            "isDraft": False,
                        }
                    ],
                    {"data": {"repository": {"pullRequest": detail}}},
                    {"id": "t_analyze"},
                    {"id": "t_fix"},
                    {"id": "t_verify"},
                ]
            )
            controller = Controller(Config.load(path), runner=runner)  # type: ignore[arg-type]
            controller.store.save(
                PRState(
                    repository="AnikaWilliams/example",
                    number=42,
                    updated_at="2026-08-10T00:00:00Z",
                    head_sha=HEAD,
                    policy_revision=1,
                )
            )

            events = controller.run(dry_run=False, verbose=False)

            state = controller.store.load("AnikaWilliams/example", 42)
            self.assertIsNotNone(state)
            self.assertEqual(
                state.pipeline_tasks() if state else {},
                {"analyze": "t_analyze", "fix": "t_fix", "verify": "t_verify"},
            )
            self.assertEqual(state.active_task_id if state else None, "t_verify")
            self.assertEqual(state.active_task_status if state else None, "scheduled")
            self.assertTrue(any("created MoA pipeline cards" in event.message for event in events))
            created = [call for call in runner.json_calls if call[:3] == ["hermes", "kanban", "create"]]
            self.assertEqual(len(created), 3)

    def test_blocked_upstream_pipeline_stage_keeps_the_controller_safely_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            runner = RecordingRunner(
                [{"status": "done"}, {"status": "blocked"}, {"status": "scheduled"}]
            )
            controller = Controller(Config.load(path), runner=runner)  # type: ignore[arg-type]
            state = PRState(
                repository="AnikaWilliams/example",
                number=42,
                updated_at="2026-08-10T00:00:00Z",
                head_sha=HEAD,
                active_task_id="t_verify",
                active_task_status="scheduled",
                pipeline_json='{"analyze":"t_analyze","fix":"t_fix","verify":"t_verify"}',
            )

            observed, changed, statuses = controller._observe_pipeline(state)

            self.assertTrue(changed)
            self.assertEqual(observed.active_task_status, "blocked")
            self.assertEqual(statuses, {"analyze": "done", "fix": "blocked", "verify": "scheduled"})

    def test_nonterminal_upstream_pipeline_stage_prevents_terminal_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            runner = RecordingRunner(
                [{"status": "done"}, {"status": "todo"}, {"status": "done"}]
            )
            controller = Controller(Config.load(path), runner=runner)  # type: ignore[arg-type]
            state = PRState(
                repository="AnikaWilliams/example",
                number=42,
                updated_at="2026-08-10T00:00:00Z",
                head_sha=HEAD,
                active_task_id="t_verify",
                active_task_status="scheduled",
                pipeline_json='{"analyze":"t_analyze","fix":"t_fix","verify":"t_verify"}',
            )

            observed, changed, statuses = controller._observe_pipeline(state)

            self.assertTrue(changed)
            self.assertEqual(observed.active_task_status, "todo")
            self.assertEqual(statuses, {"analyze": "done", "fix": "todo", "verify": "done"})


class ControllerLockLivenessTests(unittest.TestCase):
    def test_windows_liveness_delegates_without_signalling_the_process(self) -> None:
        with (
            patch.object(pr_autopilot.os, "name", "nt"),
            patch.object(
                pr_autopilot,
                "_windows_process_is_running",
                side_effect=[False, True],
            ) as liveness,
            patch.object(pr_autopilot.os, "kill") as kill,
        ):
            self.assertFalse(pr_autopilot._process_is_running(1001))
            self.assertTrue(pr_autopilot._process_is_running(1002))

        self.assertEqual(liveness.call_args_list, [call(1001), call(1002)])
        kill.assert_not_called()

    @unittest.skipUnless(hasattr(ctypes, "WinDLL"), "requires Windows ctypes")
    def test_windows_liveness_keeps_ambiguous_processes_live(self) -> None:
        kernel32 = MagicMock()
        kernel32.OpenProcess.return_value = 0
        with (
            patch("ctypes.WinDLL", return_value=kernel32),
            patch("ctypes.get_last_error", return_value=87),
        ):
            self.assertFalse(pr_autopilot._windows_process_is_running(1001))
        kernel32.OpenProcess.assert_called_with(0x1000, False, 1001)

        with (
            patch("ctypes.WinDLL", return_value=kernel32),
            patch("ctypes.get_last_error", return_value=5),
        ):
            self.assertTrue(pr_autopilot._windows_process_is_running(1002))

        with patch("ctypes.WinDLL", side_effect=OSError("query unavailable")):
            self.assertTrue(pr_autopilot._windows_process_is_running(1003))

    def test_stale_lock_loser_keeps_a_replacement_owner_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            lock_path = root / ".pr-autopilot.lock"
            lock_path.write_text("0", encoding="utf-8")
            entered_first_reclaimer = threading.Event()
            release_first_reclaimer = threading.Event()
            original_try_lock = pr_autopilot._try_lock_controller_file
            calls = 0
            loser_results: list[bool] = []

            def pause_first_reclaimer(descriptor: int) -> bool:
                nonlocal calls
                calls += 1
                if calls == 1:
                    entered_first_reclaimer.set()
                    self.assertTrue(release_first_reclaimer.wait(timeout=5))
                return original_try_lock(descriptor)

            with patch.object(
                pr_autopilot,
                "_try_lock_controller_file",
                side_effect=pause_first_reclaimer,
            ):
                loser = threading.Thread(
                    target=lambda: loser_results.append(
                        pr_autopilot._reclaim_stale_controller_lock(lock_path)
                    ),
                )
                loser.start()
                self.assertTrue(entered_first_reclaimer.wait(timeout=5))

                self.assertTrue(pr_autopilot._reclaim_stale_controller_lock(lock_path))
                replacement = pr_autopilot._acquire_controller_lock(config_path)
                self.assertIsNotNone(replacement)
                assert replacement is not None
                os.lseek(replacement[0], 0, os.SEEK_SET)
                self.assertEqual(os.read(replacement[0], 64).decode("ascii"), str(os.getpid()))

                release_first_reclaimer.set()
                loser.join(timeout=5)
                self.assertFalse(loser.is_alive())
                self.assertEqual(loser_results, [True])
                self.assertTrue(pr_autopilot._descriptor_matches_lock_path(*replacement))

            assert replacement is not None
            pr_autopilot._release_controller_lock(replacement)


class EndpointReadinessTests(unittest.TestCase):
    def test_endpoint_readiness_reprobes_each_recovery_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            outcomes = iter((False, True, True, False))
            calls: list[str] = []
            endpoint = "https://spark.example.test/v1/"
            controller = Controller(
                Config.load(path),
                runner=RecordingRunner([]),  # type: ignore[arg-type]
                endpoint_probe=lambda value: calls.append(value) or next(outcomes),
            )

            self.assertFalse(controller._endpoint_is_ready(endpoint))
            self.assertTrue(controller._endpoint_is_ready(endpoint))
            self.assertTrue(controller._endpoint_is_ready(endpoint))
            self.assertFalse(controller._endpoint_is_ready(endpoint))
            self.assertEqual(calls, [endpoint] * 4)


if __name__ == "__main__":
    unittest.main()
