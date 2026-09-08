import argparse
import json
import subprocess
import sys
from pathlib import Path
from .io import load_records, write_report
from .hubspot import HubSpotError, fetch_private_run, canonical_records, write_private_run
from .audit import write_approval_record
from .mapping import map_row
from .redaction import assert_public_safe
from .build import BuildConfigError, BuildPathError, build as build_artifact_tree
from .private_run import PrivateRunError, run_private_run
from .public_proof import PublicProofError, build_public_proof
from .source_stage import SourceStageError, run_source_stage


def root():
    return Path(__file__).resolve().parent.parent


def demo_rows():
    """Embedded synthetic data keeps the installed demo independent of source fixtures."""
    return [
        {
            "id": "demo-trial",
            "email": "trial@example.invalid",
            "trial_end_days": 7,
            "value": 1000,
            "subscribed": True,
            "consent_email": True,
        },
        {
            "id": "demo-optout",
            "email": "optout@example.invalid",
            "last_activity_days": 90,
            "value": 500,
            "subscribed": False,
            "consent_email": True,
        },
        {"id": "demo-review", "last_purchase_days": 120, "value": 300, "subscribed": True},
    ]


def cmd_demo(_):
    output = Path.cwd() / "output" / "sample-public.json"
    write_report([map_row(row) for row in demo_rows()], Path.cwd(), "output/sample-public.json")
    print(output)
    return 0


def cmd_run(args):
    approved = []
    if args.approval_file:
        approved = json.loads(Path(args.approval_file).read_text(encoding="utf-8")).get(
            "approved_tokens", []
        )
        if not isinstance(approved, list):
            raise ValueError("approved_tokens must be a list")
        if args.audit_log:
            write_approval_record(Path.cwd(), args.audit_log, approved, args.approved_by)
    write_report(load_records(args.input), Path.cwd(), args.output, approved)
    print(args.output)
    return 0


def cmd_guide(args):
    from found_money.profile.cli import guided_session

    return guided_session(args.output_root)


def cmd_hubspot_analyze(args):
    try:
        data = fetch_private_run()
    except HubSpotError as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 2
    out = Path(args.output).resolve()
    write_private_run(data, out, Path.cwd() / "private-runs")
    report = out / "public-safe-report.json"
    write_report([map_row(row) for row in canonical_records(data)], out, report.name)
    print(report)
    return 0


def cmd_doctor(args):
    source_tests = root() / "tests" / "test_found_money.py"
    if source_tests.exists():
        check = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=root()
        ).returncode
    else:
        report = __import__("found_money.io", fromlist=["public_report"]).public_report(
            [map_row(row) for row in demo_rows()]
        )
        check = (
            0
            if report["no_send_enforced"]
            and report["proof_tier"] == "built"
            and assert_public_safe(report)
            else 1
        )
    demo = cmd_demo(args) if check == 0 else 1
    ok = check == 0 and demo == 0
    print("doctor: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def cmd_build(args):
    try:
        result = build_artifact_tree(
            output_root=args.output_root,
            source_config=args.source_config,
            hubspot_snapshot=args.hubspot_snapshot,
            stripe_snapshot=args.stripe_snapshot,
            credential_declaration=args.credential_declaration,
        )
    except (BuildConfigError, BuildPathError) as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 2
    except Exception:
        print("error: build failed", file=sys.stderr)
        return 1
    print(f"found-money build: completed ({result.run_id})")
    return 0


def cmd_private_run(args):
    try:
        result = run_private_run(
            config_path=args.config,
            private_root=args.private_root,
        )
    except PrivateRunError as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 2
    except Exception:
        print("error: private-run failed", file=sys.stderr)
        return 1
    print(f"found-money private-run: completed ({result.run_id})")
    return 0


def cmd_public_proof(args):
    try:
        result = build_public_proof(
            output_root=args.output_root,
            config_path=args.config,
            fm036_aggregate_path=args.fm036_aggregate,
        )
    except PublicProofError as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 2
    except Exception:
        print("error: public-proof failed", file=sys.stderr)
        return 1
    print(f"found-money public-proof: completed ({result.run_id})")
    return 0


def cmd_source_stage(args):
    try:
        result = run_source_stage(
            args.manifest,
            input_root=args.input_root,
            output_root=args.output_root,
        )
    except SourceStageError as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 2
    print(f"found-money source-stage: completed ({result.source_set_hash})")
    return 0


def main():
    parser = argparse.ArgumentParser(prog="found_money")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor").set_defaults(func=cmd_doctor)
    sub.add_parser("demo").set_defaults(func=cmd_demo)
    build = sub.add_parser("build")
    build.add_argument("--output-root", "--output", dest="output_root", required=True)
    build.add_argument("--source-config", "--config", dest="source_config")
    build.add_argument("--hubspot-snapshot", "--crm-snapshot", dest="hubspot_snapshot")
    build.add_argument("--stripe-snapshot", "--billing-snapshot", dest="stripe_snapshot")
    build.add_argument("--credential-declaration", choices=["none"])
    build.set_defaults(func=cmd_build)
    private_run = sub.add_parser("private-run")
    private_run.add_argument("--config", dest="config", required=True)
    private_run.add_argument("--private-root", "--output-root", dest="private_root", required=True)
    private_run.set_defaults(func=cmd_private_run)
    public_proof = sub.add_parser("public-proof")
    public_proof.add_argument("--output-root", "--output", dest="output_root", required=True)
    public_proof.add_argument("--config", dest="config")
    public_proof.add_argument("--fm036-aggregate", dest="fm036_aggregate", default=None)
    public_proof.set_defaults(func=cmd_public_proof)
    source_stage = sub.add_parser("source-stage")
    source_stage.add_argument("--manifest", "--config", dest="manifest", required=True)
    source_stage.add_argument("--input-root")
    source_stage.add_argument("--output-root", "--output", dest="output_root", required=True)
    source_stage.set_defaults(func=cmd_source_stage)
    run = sub.add_parser("run")
    run.add_argument("--input", required=True)
    run.add_argument("--output", required=True)
    run.add_argument("--approval-file")
    run.add_argument("--audit-log")
    run.add_argument("--approved-by", default="human")
    run.set_defaults(func=cmd_run)
    hs = sub.add_parser("hubspot-analyze")
    hs.add_argument("--output", required=True)
    hs.set_defaults(func=cmd_hubspot_analyze)
    guide = sub.add_parser("guide")
    guide.add_argument("--output-root", "--output", dest="output_root", required=True)
    guide.set_defaults(func=cmd_guide)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
