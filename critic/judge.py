from __future__ import annotations

from pydantic import ValidationError

from critic.backend import BackendReplyError, ModelBackend, ModelRequest
from critic.diff import PatchLine, PullRequestDiff, find_lines_ending_at, parse_patch, render_patch_line
from critic.labels import ReviewerComment
from critic.matching import LINE_WINDOW, CandidatePair
from critic.predictions import PredictedComment
from critic.prompt import REVIEWER_LOGIN
from critic.records import CriticRecord, UncheckedModelAnswer
from critic.worked_examples import JudgeExample, QuotedComment, load_worked_examples

JUDGE_BATCH_SIZE = 40
CODE_CONTEXT_LINES = 3
OUTSIDE_THE_DIFF = "(this line is outside the diff)"

_ROLE = """\
YOUR PLACE
You are the judge in an evaluation of a code-review critic for the GitHub repository {repo}. \
The critic read a pull request's diff and predicted the inline comments its reviewer, GitHub \
user {reviewer}, would leave. Each pair below holds one comment the reviewer really left and \
one the critic predicted, on the same file, that the stricter rule (within {window} lines of \
one commit, same theme) did not already match. You decide, pair by pair, whether the two make \
the same point.

WHAT BECOMES OF YOUR ANSWER
A pair you call the same point counts as a catch for the critic and raises both its precision \
and its recall. Those numbers decide whether the critic may later hold pull requests until an \
agent answers it: a generous call inflates the critic, a strict one hides what it caught.

WHAT YOU ARE SHOWN, AND WHAT NOT
Each comment comes with the few diff lines it sits under, its own line last. A reviewer comment \
left on another commit of the pull request carries that commit's lines and numbering, so \
compare the code, not the numbers. You are not shown the rest of the diff, the rest of the \
review, or which comments the critic already matched.

THE TEST
Two comments make the same point when acting on one would settle the other: the same defect, \
or the same change asked for, in any words. Nearby lines alone do not make the same point, and \
neither does a shared topic.

ANSWER
One verdict per pair_id: same_point true or false, and a one-sentence reason."""


class JudgeVerdict(CriticRecord):
    pair_id: int
    same_point: bool
    reason: str


class JudgeAnswer(CriticRecord):
    verdicts: list[JudgeVerdict]


class JudgeOutcome(CriticRecord):
    verdicts: list[JudgeVerdict]
    cost_usd: float
    model_ids: list[str]


class PairSide(CriticRecord):
    author: str
    line: int
    code: str
    body: str


def judge_candidates(
    candidates: list[CandidatePair],
    predictions: list[PredictedComment],
    reals: list[ReviewerComment],
    diff: PullRequestDiff,
    backend: ModelBackend,
) -> JudgeOutcome:
    examples = load_worked_examples().judge
    verdicts: list[JudgeVerdict] = []
    cost_usd = 0.0
    model_ids: set[str] = set()
    for start in range(0, len(candidates), JUDGE_BATCH_SIZE):
        batch = candidates[start : start + JUDGE_BATCH_SIZE]
        reply = backend.ask(build_judge_request(batch, predictions, reals, diff, examples))
        answer = read_judge_answer(reply.answer)
        validate_one_verdict_per_pair(batch, answer.verdicts)
        verdicts += answer.verdicts
        cost_usd += reply.cost_usd
        model_ids.update(reply.model_ids)
    return JudgeOutcome(verdicts=verdicts, cost_usd=cost_usd, model_ids=sorted(model_ids))


def build_judge_request(
    batch: list[CandidatePair],
    predictions: list[PredictedComment],
    reals: list[ReviewerComment],
    diff: PullRequestDiff,
    examples: list[JudgeExample],
) -> ModelRequest:
    role = _ROLE.format(repo=diff.repo, reviewer=REVIEWER_LOGIN, window=LINE_WINDOW)
    pairs = [
        render_pair(
            pair.pair_id,
            reals[pair.real_index].path,
            read_real_side(reals[pair.real_index], diff.commit_sha),
            read_prediction_side(predictions[pair.prediction_index], diff),
        )
        for pair in batch
    ]
    return ModelRequest(
        system="\n\n".join([role, render_judge_examples(examples)]),
        user="PAIRS\n\n" + "\n\n".join(pairs),
        answer_schema=JudgeAnswer.model_json_schema(),
    )


def read_judge_answer(answer: UncheckedModelAnswer) -> JudgeAnswer:
    try:
        return JudgeAnswer.model_validate(answer)
    except ValidationError as error:
        raise BackendReplyError(f"the judge answer does not fit its schema: {error}") from error


def validate_one_verdict_per_pair(batch: list[CandidatePair], verdicts: list[JudgeVerdict]) -> None:
    asked = sorted(pair.pair_id for pair in batch)
    answered = sorted(verdict.pair_id for verdict in verdicts)
    if asked != answered:
        raise BackendReplyError(f"the judge answered pairs {answered}, but was asked {asked}")


def read_real_side(real: ReviewerComment, reviewed_commit: str) -> PairSide:
    author = "reviewer" if real.commit_sha == reviewed_commit else "reviewer, on another commit"
    code = _render_code(parse_patch(real.diff_hunk)[-CODE_CONTEXT_LINES:])
    return PairSide(author=author, line=real.line, code=code, body=real.body)


def read_prediction_side(prediction: PredictedComment, diff: PullRequestDiff) -> PairSide:
    lines = find_lines_ending_at(diff, prediction.path, prediction.line, CODE_CONTEXT_LINES)
    code = _render_code(lines) if lines else OUTSIDE_THE_DIFF
    return PairSide(author="critic", line=prediction.line, code=code, body=prediction.text)


def render_judge_examples(examples: list[JudgeExample]) -> str:
    intro = (
        "WORKED EXAMPLES\nEach pair is two real comments the reviewer left on one file; "
        "the second stands where the critic's comment would."
    )
    blocks = [render_judge_example(number, example) for number, example in enumerate(examples, 1)]
    return "\n\n".join([intro, *blocks])


def render_judge_example(number: int, example: JudgeExample) -> str:
    verdict = JudgeVerdict(pair_id=number, same_point=example.same_point, reason=example.reason)
    reviewer = _read_quoted_side(example.reviewer, "reviewer")
    critic = _read_quoted_side(example.other, "critic")
    pair = render_pair(number, example.reviewer.path, reviewer, critic)
    sources = f"Sources: {example.reviewer.html_url} and {example.other.html_url}"
    return f"{sources}\n{pair}\nVerdict: {verdict.model_dump_json()}"


def render_pair(pair_id: int, path: str, reviewer: PairSide, critic: PairSide) -> str:
    return "\n".join([f"pair_id {pair_id}, file {path}", _render_side(reviewer), _render_side(critic)])


def _render_side(side: PairSide) -> str:
    return (
        f"  {side.author}, line {side.line}\n"
        f"    code:\n{_indent(side.code, 6)}\n"
        f"    comment: {_indent(side.body.strip(), 6).lstrip()}"
    )


def _read_quoted_side(quoted: QuotedComment, author: str) -> PairSide:
    code = _render_code(parse_patch(quoted.diff_hunk)[-CODE_CONTEXT_LINES:])
    return PairSide(author=author, line=quoted.line, code=code, body=quoted.body)


def _render_code(lines: list[PatchLine]) -> str:
    return "\n".join(render_patch_line(line) for line in lines)


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line for line in text.splitlines())
