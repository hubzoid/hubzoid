"""Configure this GitZoid Hub with selected defaults; secrets are prompted, never CLI arguments."""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import shutil

import yaml
from dotenv import set_key


def configure(hub: Path, repos: list[str], *, publish: bool = False, backlog: str = "all",
              drafts: bool = False, bots: bool = False, status: str = "report", email: str = "") -> dict:
    import importlib.util
    import sys
    root = hub / "workflows" / "gitzoid" / "lib"
    name = "gitzoid_setup_" + str(abs(hash(str(root))))
    spec = importlib.util.spec_from_file_location(name, root / "package.py", submodule_search_locations=[str(root)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    path = hub / "workflows" / "settings.yaml"
    data = yaml.safe_load(path.read_text()) or {}
    old = data.get("gitzoid") or {}
    # Retain user rules, model choices and budgets. Replace only the repo/group selection when connecting.
    selected = list(dict.fromkeys(repos))
    if not selected:
        raise ValueError("Choose at least one repository (owner/name)")
    if email and ("@" not in email or any(c.isspace() for c in email)):
        raise ValueError("Email must be one address")
    source_changed = old.get("source") != "github"
    existing_repos = {r["name"]: r for r in old.get("repos", []) if isinstance(r, dict)}
    config = {**old, "source": "github", "repos": [existing_repos.get(r, r) for r in selected],
              "review": {**old.get("review", {}), "post_to_github": publish, "backlog": backlog,
                         "skip_drafts": not drafts, "skip_bots": not bots, "status_check": status}}
    if source_changed:
        config["security_groups"] = {"team": {"repos": selected, "email_list": "gitzoid-owner"}}
        config["digest_groups"] = {"team": {"repos": selected, "email_list": "gitzoid-owner",
                                                    "monthly_reports": ["code", "delivery"]}}
        config["owner_list"] = "gitzoid-owner"
    else:
        # Removed repos must not leave invalid group references; new repos get the default group.
        for kind in ("security_groups", "digest_groups"):
            groups = {n: {**g, "repos": [r for r in g.get("repos", []) if r in selected]}
                      for n, g in config.get(kind, {}).items()}
            groups = {n: g for n, g in groups.items() if g["repos"]}
            assigned = {r for g in groups.values() for r in g["repos"]}
            rest = [r for r in selected if r not in assigned]
            if rest:
                name = "new-repos"
                while name in groups:
                    name += "-next"
                groups[name] = {"repos": rest, "email_list": config.get("owner_list")}
                if kind == "digest_groups":
                    groups[name]["monthly_reports"] = ["code", "delivery"]
            config[kind] = groups
    module.settings.load(config)
    data["gitzoid"] = config
    if email:
        data.setdefault("email_lists", {})["gitzoid-owner"] = [email]
    backup = path.with_suffix(".yaml.before-setup")
    # Do not overwrite the original backup on a repeated setup.
    if not backup.exists():
        shutil.copy2(path, backup)
    temp = path.with_suffix(".yaml.tmp")
    temp.write_text(yaml.safe_dump(data, sort_keys=False))
    temp.replace(path)
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repos", nargs="+", help="GitHub owner/name repositories")
    parser.add_argument("--publish", action=argparse.BooleanOptionalAction, default=False,
                        help="Post GitHub reviews; default previews only")
    parser.add_argument("--backlog", choices=("all", "latest", "new"), default="all")
    parser.add_argument("--drafts", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--bots", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--status", choices=("report", "gate", "off"), default="report")
    parser.add_argument("--email", default="", help="Optional report recipient; must be allowed by Hubzoid")
    parser.add_argument("--save-token", action="store_true", help="Prompt privately for a fine-grained PAT and save it in .env")
    args = parser.parse_args()
    repos = args.repos
    if not repos:
        if not os.isatty(0):
            parser.error("--repos is required when running without a terminal")
        repos = input("GitHub repos (owner/name, separated by spaces): ").split()
    hub = Path(__file__).resolve().parent
    try:
        config = configure(hub, repos, publish=args.publish, backlog=args.backlog,
                           drafts=args.drafts, bots=args.bots, status=args.status, email=args.email)
    except ValueError as exc:
        parser.error(str(exc))
    if args.save_token:
        token = getpass.getpass("GitHub fine-grained token (hidden): ").strip()
        if not token or any(c.isspace() for c in token):
            parser.error("Token must be a nonempty single value")
        env = hub / ".env"
        env.touch(mode=0o600, exist_ok=True)
        env.chmod(0o600)
        set_key(str(env), "GITHUB_TOKEN", token)
    import shlex
    name = shlex.quote(str(hub))
    print(f"Configured {len(config['repos'])} repo(s). "
          f"{'GitHub publishing enabled' if args.publish else 'Preview mode: nothing posted to GitHub'}. "
          f"Backlog: {args.backlog}. Status: {args.status}.")
    print("Next commands (keep the last one running for automatic reviews):")
    for workflow in ("setup_check", "repo_profiles", "pr_sweep"):
        print(f"  hubzoid schedule run {name} {workflow}")
    print(f"  hubzoid run {name}")
    print("All review, security and report workflows are available. Email is optional. Advanced options: workflows/settings.yaml.")


if __name__ == "__main__":
    main()
