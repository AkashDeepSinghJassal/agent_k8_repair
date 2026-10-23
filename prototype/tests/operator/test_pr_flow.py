"""Drives Collected -> ... -> Merged/Failed/Escalated against an in-memory fake GitHub."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

from repair_operator import git_writer, repair_flow
from repair_operator.analyzer import InvalidOutput
from repair_operator.config import Settings
from repair_operator.knowledge import RecipeStore
from repair_operator.models import Edit, Proposal

FIX = Path(__file__).parent / "fixtures" / "api.yaml"
PATH = "deploy/demo/api.yaml"
NAME = "ri-abc1234567"
SKIP = ("static", "server_dry_run", "policy")


# ---- fake GitHub -------------------------------------------------------------


class FakePR:
    def __init__(self, repo, number, head, title, body):
        self.repo, self.number, self.head, self.title, self.body = repo, number, head, title, body
        self.state, self.merged, self.merge_commit_sha = "open", False, None
        self.labels: list[str] = []
        self.issue_comments: list[SimpleNamespace] = []
        self.reviews: list[SimpleNamespace] = []
        self.review_comments: list[SimpleNamespace] = []

    html_url = property(lambda self: f"https://github.com/o/r/pull/{self.number}")

    def add_to_labels(self, *names):
        self.labels += names

    def get_labels(self):
        return [SimpleNamespace(name=n) for n in self.labels]

    def edit(self, body=None, **_):
        self.body = body

    def create_issue_comment(self, body, user="bot"):
        c = SimpleNamespace(id=self.repo.next_id(), body=body, user=user)
        self.issue_comments.append(c)
        return c

    def get_issue_comments(self):
        return list(self.issue_comments)

    def get_reviews(self):
        return list(self.reviews)

    def get_review_comments(self):
        return list(self.review_comments)

    # test helpers (the human side)
    def review(self, state, body="", lines=()):
        rid = self.repo.next_id()
        self.reviews.append(SimpleNamespace(id=rid, state=state, body=body))
        for path, line, text in lines:
            self.review_comments.append(
                SimpleNamespace(id=self.repo.next_id(), pull_request_review_id=rid, path=path, line=line, body=text)
            )

    def merge(self):
        self.state, self.merged, self.merge_commit_sha = "closed", True, "mergesha1"


class FakeRepo:
    default_branch = "main"
    owner = SimpleNamespace(login="o")
    full_name = "o/r"

    def __init__(self, files):
        self.branches = {"main": dict(files)}
        self.prs: list[FakePR] = []
        self.commits: list[tuple[str, str]] = []
        self._id = 0

    def next_id(self):
        self._id += 1
        return self._id

    def get_branch(self, name):
        if name not in self.branches:
            raise KeyError(name)
        return SimpleNamespace(commit=SimpleNamespace(sha=f"sha-{name}"))

    def create_git_ref(self, ref, sha):
        name = ref.removeprefix("refs/heads/")
        self.branches[name] = dict(self.branches["main"])

    def get_contents(self, path, ref=None):
        text = self.branches[ref or "main"][path]
        return SimpleNamespace(decoded_content=text.encode(), sha=f"blob-{hash(text)}")

    def update_file(self, path, message, content, sha, branch):
        self.branches[branch][path] = content
        self.commits.append((branch, message))

    def get_pulls(self, state="open", head=None):
        return [p for p in self.prs if (head is None or head == f"o:{p.head}") and state in ("all", p.state)]

    def get_git_ref(self, ref):
        name = ref.removeprefix("heads/")
        return SimpleNamespace(delete=lambda: self.branches.pop(name))

    def get_pull(self, n):
        return next(p for p in self.prs if p.number == n)

    def create_pull(self, title, body, head, base, draft=False):
        pr = FakePR(self, len(self.prs) + 1, head, title, body)
        self.prs.append(pr)
        return pr


# ---- harness -----------------------------------------------------------------


def merge_patch(dst: dict, src: dict):
    for k, v in src.items():
        if v is None:
            dst.pop(k, None)
        elif isinstance(v, dict) and isinstance(dst.get(k), dict):
            merge_patch(dst[k], v)
        else:
            dst[k] = v


def proposal(find="memory: 128Mi", replace="memory: 256Mi", tier=3, fixable=True):
    return Proposal(
        **{"class": "CRASH_LOOP"}, root_cause="Container is out of memory.", confidence=0.9,
        fixable_in_git=fixable, file=PATH if fixable else None, edits=[Edit(find=find, replace=replace)] if fixable else [],
        rationale="raise limit", verify="pods Ready", tier=tier if fixable else 4, source="llm",
    )


def _no_cluster(*_a, **_k):
    raise RuntimeError("no cluster in unit tests")


class Env:
    def __init__(self, tmp_path, analyze_fn, **settings):
        self.settings = Settings(state_dir=tmp_path, **settings)
        (tmp_path / "evidence").mkdir()
        bundle = {"incident": NAME, "signature": "sig", "git": {"path": PATH}, "source": {"owner": "Deployment/api", "container": "api"},
                  "failure": {"reason": "OOMKilled", "exitCode": 137, "restartCount": 3, "message": "needs 1Gi or 512Mi"}, "events": []}
        (tmp_path / "evidence" / f"{NAME}-1.json").write_text(json.dumps(bundle))
        self.repo = FakeRepo({PATH: FIX.read_text()})
        self.analyze_fn = analyze_fn
        self.collect_fn = _no_cluster
        self.body = {"metadata": {"name": NAME}, "status": {"phase": "Collected", "attempts": 1, "evidenceRef": "sha256:x", "signature": "sig"}}

    def apply(self, patch):
        merge_patch(self.body["status"], patch.get("status", {}))

    def analyze(self, **kw):
        p: dict = {}
        repair_flow.analyze_step(self.body, p, repo=self.repo, analyze_fn=self.analyze_fn, guard_skip=SKIP, settings=self.settings, collect_fn=self.collect_fn, **kw)
        self.apply(p)

    def propose(self):
        p: dict = {}
        repair_flow.propose_step(self.body, p, repo=self.repo, settings=self.settings)
        self.apply(p)

    def review(self, store=None):
        p: dict = {}
        repair_flow.review_step(self.body, p, repo=self.repo, settings=self.settings, store=store)
        self.apply(p)

    @property
    def status(self):
        return self.body["status"]


def make_env(tmp_path, analyze_fn, **kw):
    mem = re.search(r"memory: (\S+)", FIX.read_text())
    assert mem, "fixture needs a memory setting"
    env = Env(tmp_path, analyze_fn, **kw)
    return env, mem.group(0)


# ---- tests -------------------------------------------------------------------


def test_happy_path_to_merged(tmp_path):
    env, cur = make_env(tmp_path, lambda b, f=None, **k: proposal(find=cur, replace="memory: 1Gi"))
    env.analyze()
    assert env.status["phase"] == "Proposed" and env.status["proposal"]["tier"] == 3
    assert env.status["classification"]["class"] == "CRASH_LOOP"
    env.propose()
    assert env.status["phase"] == "AwaitingReview"
    pr = env.repo.prs[0]
    assert env.status["pullRequest"] == pr.html_url and pr.head == f"repair/{NAME}"
    assert pr.labels == ["ai-repair", "crash_loop"] and pr.title.startswith("[repair] CRASH_LOOP:")
    assert "memory: 1Gi" in env.repo.branches[pr.head][PATH] and "memory: 1Gi" not in env.repo.branches["main"][PATH]
    assert "Repair-Incident: " + NAME in env.repo.commits[0][1]
    assert NAME in pr.body and "sha256:x" in pr.body
    env.review()  # nothing yet
    assert env.status["phase"] == "AwaitingReview"
    pr.merge()
    env.review()
    assert env.status["phase"] == "Merged" and env.status["mergeSha"] == "mergesha1"
    assert env.status["timestamps"]["merged"]


def test_changes_requested_loops_back_same_branch(tmp_path):
    seen = []

    def fn(bundle, feedback=None, **_):
        seen.append(feedback)
        return proposal(find=cur, replace="memory: 512Mi" if feedback else "memory: 1Gi")

    env, cur = make_env(tmp_path, fn)
    env.analyze()
    env.propose()
    pr = env.repo.prs[0]
    pr.review("CHANGES_REQUESTED", "too much memory", lines=[(PATH, 31, "use 512Mi")])
    pr.review("COMMENTED", "fyi")
    env.review()
    assert env.status["phase"] == "Analyzing" and env.status["attempts"] == 2
    assert env.status["reviewFeedback"] == [f"too much memory\n{PATH}:31: use 512Mi"]
    env.analyze()
    assert seen[1] == env.status["reviewFeedback"]
    env.propose()
    assert len(env.repo.prs) == 1 and env.status["phase"] == "AwaitingReview"
    assert "memory: 512Mi" in env.repo.branches[pr.head][PATH] and len(env.repo.commits) == 2
    env.review()  # same review must not trigger again
    assert env.status["phase"] == "AwaitingReview"


def test_retry_comment_is_changes_requested_and_bot_comments_ignored(tmp_path):
    env, cur = make_env(tmp_path, lambda b, f=None, **k: proposal(find=cur, replace="memory: 1Gi"))
    env.analyze()
    env.propose()
    pr = env.repo.prs[0]
    pr.create_issue_comment(f"{git_writer.BOT_MARKER}\n/repair retry from the bot")
    pr.create_issue_comment("looks fine to me")
    env.review()
    assert env.status["phase"] == "AwaitingReview"
    pr.create_issue_comment("/repair retry keep it under 512Mi", user="human")
    env.review()
    assert env.status["phase"] == "Analyzing" and env.status["reviewFeedback"] == ["keep it under 512Mi"]


def test_closed_without_merge_fails(tmp_path):
    env, cur = make_env(tmp_path, lambda b, f=None, **k: proposal(find=cur, replace="memory: 1Gi"))
    env.analyze()
    env.propose()
    env.repo.prs[0].state = "closed"
    env.review()
    assert env.status["phase"] == "Failed" and env.status["outcome"]["reason"] == "rejected_by_reviewer"


def test_attempts_exceeded_escalates_with_label_and_single_comment(tmp_path):
    env, cur = make_env(tmp_path, lambda b, f=None, **k: proposal(find=cur, replace="memory: 1Gi"), max_attempts=2)
    env.analyze()
    env.propose()
    pr = env.repo.prs[0]
    pr.review("CHANGES_REQUESTED", "no")
    env.review()
    env.analyze()
    env.propose()
    pr.review("CHANGES_REQUESTED", "still no")
    env.review()
    assert env.status["attempts"] == 3
    env.analyze()
    assert env.status["phase"] == "Escalated" and env.status["outcome"]["reason"] == "max_attempts"
    assert "needs-human" in pr.labels
    n = len(pr.issue_comments)
    repair_flow._escalate({}, "again", None, env.repo, env.status)
    assert len(pr.issue_comments) == n


def test_invalid_output_retries_then_escalates(tmp_path):
    calls = []

    def fn(b, f=None, **k):
        calls.append(1)
        raise InvalidOutput("bad")

    env, _ = make_env(tmp_path, fn, max_attempts=3)
    env.analyze()
    assert len(calls) == 3
    assert env.status["phase"] == "Escalated" and env.status["outcome"]["reason"] == "invalid_output"
    assert not env.repo.prs


def test_invalid_output_then_success(tmp_path):
    calls = []

    def fn(b, f=None, **k):
        calls.append(1)
        if len(calls) == 1:
            raise InvalidOutput("bad")
        return proposal(find=cur, replace="memory: 1Gi")

    env, cur = make_env(tmp_path, fn)
    env.analyze()
    assert env.status["phase"] == "Proposed" and env.status["attempts"] == 2


def test_guardrail_rejection_retried_with_feedback(tmp_path):
    seen = []

    def fn(b, f=None, **k):
        seen.append(f)
        # first proposal's find string does not exist -> exact_edit rejection
        return proposal(find="nope: nope" if not f else cur, replace="memory: 1Gi")

    env, cur = make_env(tmp_path, fn)
    env.analyze()
    assert env.status["phase"] == "Proposed" and env.status["attempts"] == 2
    assert "exact_edit" in env.status["reviewFeedback"][0] and seen[1]
    assert env.status["proposal"]["rejectedBy"] is None


def test_guardrail_always_rejected_escalates(tmp_path):
    env, _ = make_env(tmp_path, lambda b, f=None, **k: proposal(find="nope: nope", replace="x"), max_attempts=2)
    env.analyze()
    assert env.status["phase"] == "Escalated" and env.status["outcome"]["reason"] == "guardrail:exact_edit"
    assert env.status["proposal"]["rejectedBy"] == "exact_edit"


def test_tier4_escalates_without_pr(tmp_path):
    env, _ = make_env(tmp_path, lambda b, f=None, **k: proposal(fixable=False))
    env.analyze()
    assert env.status["phase"] == "Escalated" and env.status["outcome"]["reason"] == "not_fixable_in_git"
    assert not env.repo.prs


def test_shadow_mode_no_pr(tmp_path):
    env, cur = make_env(tmp_path, lambda b, f=None, **k: proposal(find=cur, replace="memory: 1Gi"), shadow_mode=True)
    env.analyze()
    assert env.status["shadow"] is True and env.status["phase"] == "Escalated"
    assert env.status["outcome"]["reason"] == "shadow_mode" and not env.repo.prs


def test_recipe_outcomes_recorded(tmp_path):
    store = RecipeStore(tmp_path / "k.db")
    p = proposal(find="x", replace="y")
    p = p.model_copy(update={"template": "raise_memory", "params": {"a": 1}})
    env, _ = make_env(tmp_path, lambda b, f=None, **k: p)
    env.status["proposal"] = repair_flow._proposal_status(p)
    env.status["pullRequest"] = "https://github.com/o/r/pull/1"
    env.status["phase"] = "AwaitingReview"
    env.repo.prs.append(FakePR(env.repo, 1, "b", "t", "b"))
    env.repo.prs[0].merge()
    env.review(store=store)
    assert store.get("sig").approved == 1
    assert env.status["phase"] == "Merged"


def test_main_handlers_registered():
    from repair_operator import main

    assert callable(main.on_analyzing) and callable(main.on_proposed) and callable(main.on_review_poll)


def _bundle(name, marker):
    return {"incident": name, "signature": "sig", "git": {"path": PATH}, "marker": marker, "note": "1Gi 512Mi",
            "source": {"owner": "Deployment/api", "container": "api"}, "events": []}


def test_load_bundle_prefers_current_attempt_then_newest(tmp_path):
    env, _ = make_env(tmp_path, lambda b, f=None, **k: proposal())
    ev = tmp_path / "evidence"
    (ev / f"{NAME}-3.json").write_text(json.dumps(_bundle(NAME, "three")))
    assert repair_flow.load_bundle(NAME, 1, env.settings).get("marker") is None  # exact attempt 1
    assert repair_flow.load_bundle(NAME, 3, env.settings)["marker"] == "three"
    assert repair_flow.load_bundle(NAME, 2, env.settings)["marker"] == "three"  # no -2: newest
    assert repair_flow.load_bundle("other", 1, env.settings) is None


def test_reanalysis_collects_fresh_bundle_and_uses_it(tmp_path):
    seen, collected = [], []

    def fn(bundle, feedback=None, **_):
        seen.append(bundle.get("marker"))
        return proposal(find=cur, replace="memory: 1Gi")

    def collect(core, custom, gh, incident, *, attempt, state_dir):
        collected.append(attempt)
        (state_dir / "evidence" / f"{NAME}-{attempt}.json").write_text(json.dumps(_bundle(NAME, f"fresh-{attempt}")))
        return {"evidenceRef": f"sha256:fresh{attempt}", "signature": "sig"}

    env, cur = make_env(tmp_path, fn)
    env.collect_fn = collect
    env.analyze()  # attempt 1: the original bundle, nothing collected
    assert collected == [] and seen == [None]
    env.propose()
    env.repo.prs[0].review("CHANGES_REQUESTED", "no")
    env.review()
    assert env.status["attempts"] == 2
    env.analyze()
    assert collected == [2] and seen[-1] == "fresh-2"
    assert (tmp_path / "evidence" / f"{NAME}-2.json").exists()
    assert env.status["evidenceRef"] == "sha256:fresh2"


def test_collection_failure_does_not_block_analysis(tmp_path):
    seen = []

    def fn(bundle, feedback=None, **_):
        seen.append(bundle["failure"]["reason"])
        return proposal(find=cur, replace="memory: 1Gi")

    env, cur = make_env(tmp_path, fn)
    env.status["attempts"] = 2  # a re-analysis, collection raises
    env.analyze()
    assert env.status["phase"] == "Proposed" and seen == ["OOMKilled"]


def test_pr_poll_interval_is_30_seconds():
    from repair_operator import main

    assert main.PR_POLL_SECONDS == 30
    assert getattr(main.on_review_poll, "__name__", "") == "on_review_poll"


def test_llm_unavailable_escalates_immediately(tmp_path):
    from repair_operator.analyzer import LlmUnavailable

    calls = []

    def fn(b, f=None, **k):
        calls.append(1)
        raise LlmUnavailable("credit balance is too low")

    env, _ = make_env(tmp_path, fn, max_attempts=3)
    env.analyze()
    assert len(calls) == 1
    assert env.status["phase"] == "Escalated"
    assert env.status["outcome"]["reason"] == "llm_unavailable: credit balance is too low"


def test_stale_merged_pr_for_same_incident_name_is_not_reused():
    from repair_operator.git_writer import open_or_update_pr

    repo = FakeRepo({PATH: "memory: 128Mi\n"})
    old = open_or_update_pr(repo, "ri-x", proposal(), "memory: 256Mi\n", 1)
    old.merge()
    new = open_or_update_pr(repo, "ri-x", proposal(), "memory: 256Mi\n", 1)
    assert new is not old and new.state == "open" and len(repo.prs) == 2


def test_stale_review_poll_does_not_regress_phase(tmp_path):
    env, cur = make_env(tmp_path, lambda b, f=None, **k: proposal(find=cur, replace="memory: 1Gi"))
    env.analyze()
    env.propose()
    env.repo.prs[0].merge()
    env.body["status"]["phase"] = "Verifying"  # the Argo-sync handler already moved on
    env.review()
    assert env.status["phase"] == "Verifying"
