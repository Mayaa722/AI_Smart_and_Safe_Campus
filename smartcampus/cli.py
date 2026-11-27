"""Command line: python -m smartcampus.cli <command> ...  (run with --help for details)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import RUNS_DIR, load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smartcampus")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="Read a lecture file, describe slides, build the index")
    p.add_argument("file")
    p.add_argument("--name")
    p.add_argument("--no-describe", action="store_true", help="Skip slide descriptions (no API calls)")

    sub.add_parser("lectures", help="List processed lectures")

    p = sub.add_parser("ask", help="Ask the tutor one question")
    p.add_argument("doc_id")
    p.add_argument("question")
    p.add_argument("--mode", default="direct", choices=["direct", "hint", "socratic", "practice"])
    p.add_argument("--version", choices=["A", "B"])
    p.add_argument("--verify", action="store_true", help="Force the claim check")

    p = sub.add_parser("summary", help="Summarize a processed lecture")
    p.add_argument("doc_id")
    p.add_argument("--version", choices=["A", "B"])

    p = sub.add_parser("quiz", help="Generate a quiz for a processed lecture")
    p.add_argument("doc_id")
    p.add_argument("--n", type=int)
    p.add_argument("--version", choices=["A", "B"])
    p.add_argument("--topic")

    p = sub.add_parser("bank", help="Quiz question bank: build (resumable), status, or draw a quiz")
    p.add_argument("action", choices=["build", "status", "quiz"])
    p.add_argument("doc_id")
    p.add_argument("--version", choices=["A", "B"])
    p.add_argument("--n", type=int, help="quiz: number of questions")
    p.add_argument("--topic", help="quiz: only questions on this topic")
    p.add_argument("--seed", type=int, help="quiz: fixed seed for a reproducible draw")

    p = sub.add_parser("search", help="Show retrieved slides for a query (no generation)")
    p.add_argument("doc_id")
    p.add_argument("query")

    p = sub.add_parser("keypoints", help="Draft lecture key points with C6 (must be human-reviewed)")
    p.add_argument("testset")

    p = sub.add_parser("check-testset", help="Validate a test-set file")
    p.add_argument("testset")

    p = sub.add_parser("eval-retrieval", help="M1: retrieval ablation ladder")
    p.add_argument("testset")
    p.add_argument("--configs", nargs="*")
    p.add_argument("--query-mode", default="raw", choices=["raw", "concat", "rewrite"])

    p = sub.add_parser("eval-features", help="M2-M4: features x models x prompt versions")
    p.add_argument("testset")
    p.add_argument("--arms", nargs="+", required=True, help="Model names from config/models.json")
    p.add_argument("--versions", nargs="+", default=["A", "B"])
    p.add_argument("--features", nargs="+", default=["explain", "summary", "quiz"])
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--quiz-questions", type=int)
    p.add_argument("--no-app-verify", action="store_true")

    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from .lecture import Lecture

    if args.command == "ingest":
        lecture = Lecture.from_file(args.file, name=args.name, describe=not args.no_describe, progress=print)
        print(json.dumps({"doc_id": lecture.doc_id, "pages": lecture.document["page_count"],
                          "described": sum(1 for p in lecture.document["pages"] if p.get("description")),
                          "warnings": lecture.document["warnings"]}, indent=1))
        lecture.retriever.index.collection  # build the index now
        print("Index ready.")
    elif args.command == "lectures":
        for path in sorted((RUNS_DIR / "lectures").glob("*/document.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            print(f"{doc['doc_id']}  {doc['page_count']:>4} pages  {doc['name']}")
    elif args.command == "ask":
        from .features.explain import ExplanationSession, answer
        turn = answer(Lecture.load(args.doc_id), ExplanationSession(), args.question, args.mode, args.version,
                      verify=True if args.verify else None)
        print(json.dumps({k: turn.get(k) for k in ("status", "error", "evidence_slides", "output")}, indent=1, ensure_ascii=False))
    elif args.command == "summary":
        from .features.summary import summarize
        record = summarize(Lecture.load(args.doc_id), args.version)
        print((record.get("output") or {}).get("body") or record["generation"]["error"])
        print(f"\nStatus: {record['status']} · saved to {record.get('saved_to')}")
    elif args.command == "quiz":
        from .features.quiz import generate
        quiz = generate(Lecture.load(args.doc_id), args.n, args.version, args.topic)
        for i, q in enumerate(quiz["questions"], 1):
            print(f"\n{i}. {q['stem']}")
            for j, o in enumerate(q["options"]):
                print(f"   {'ABCD'[j]}. {o}")
        print(f"\n{len(quiz['questions'])} released, {len(quiz['withheld'])} withheld · saved to {quiz['saved_to']}")
    elif args.command == "bank":
        from .features import bank
        lecture = Lecture.load(args.doc_id)
        if args.action == "build":
            print(json.dumps(bank.build(lecture, args.version, progress=print), indent=1))
        elif args.action == "status":
            print(json.dumps(bank.status(lecture, args.version), indent=1))
        else:
            quiz = bank.draw(lecture, args.n or lecture.settings["quiz"]["default_questions"], args.topic, seed=args.seed)
            for i, q in enumerate(quiz["questions"], 1):
                print(f"\n{i}. {q['stem']}")
                for j, o in enumerate(q["options"]):
                    print(f"   {'ABCD'[j]}. {o}")
            print(f"\n{len(quiz['questions'])} of {quiz['requested']} requested · {quiz['available']} verified "
                  f"questions available{' for this topic' if args.topic else ''}")
    elif args.command == "search":
        result =Lecture.load(args.doc_id).retriever.search(args.query)
        print(json.dumps({k: result[k] for k in ("selected", "abstain", "abstain_reason", "top_dense", "top_rerank", "seconds")}, indent=1))
    elif args.command == "keypoints":
        _keypoints(args.testset)
    elif args.command == "check-testset":
        from .evaluation.testset import load_testset
        data = load_testset(args.testset)
        types = {}
        for q in data["questions"]:
            types[q.get("type", "other")] = types.get(q.get("type", "other"), 0) + 1
        print(f"OK: {len(data['questions'])} questions {types}; lecture file: {data['_lecture_path']} "
              f"({'found' if Path(data['_lecture_path']).exists() else 'NOT FOUND'})")
    elif args.command == "eval-retrieval":
        from .evaluation.run_retrieval import run
        print("Saved:", run(args.testset, args.configs, args.query_mode)["saved_to"])
    elif args.command == "eval-features":
        from .evaluation.run_features import run
        report = run(args.testset, args.arms, tuple(args.versions), tuple(args.features), args.repeats,
                     not args.no_app_verify, args.quiz_questions)
        print("Saved:", report["saved_to"])
    return 0


def _keypoints(testset_path: str) -> None:
    from .evaluation.run_features import keypoints_path
    from .evaluation.testset import load_testset
    from .lecture import Lecture
    from .structure import topic_groups
    from .verify import build_key_points

    testset = load_testset(testset_path)
    lecture = Lecture.from_file(testset["_lecture_path"], load_settings(), testset.get("lecture_name"), progress=print)
    judge = lecture.llm("judge")
    points = []
    groups = topic_groups(lecture.content_pages(), max(1, len(lecture.content_pages()) // 6))
    for i, slides in enumerate(groups, 1):
        print(f"Section group {i}/{len(groups)}")
        result = build_key_points(judge, lecture.name, slides)
        for p in result.get("key_points", []):
            p["id"] = f"k{len(points) + 1}"
            points.append(p)
    path = keypoints_path(testset_path)
    path.write_text(json.dumps({"reviewed": False, "note": "DRAFT made by C6. A teammate must check every point "
                                "against the slides, edit/delete as needed, then set reviewed to true.",
                                "lecture": lecture.name, "key_points": points}, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Draft written to {path} ({len(points)} points). Review it, then set \"reviewed\": true.")


if __name__ == "__main__":
    from .llm import QuotaExceeded
    try:
        raise SystemExit(main())
    except QuotaExceeded as exc:
        print(f"Stopped: {exc}\nThe free daily limit for this model is used up. Try again later "
              "(cached results are reused), or switch the model in config/settings.json.", file=sys.stderr)
        raise SystemExit(2)
