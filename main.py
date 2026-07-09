import argparse
import sys
import traceback

from agent.graph import agent
from agent.tools import project_has_files


def main():
    parser = argparse.ArgumentParser(description="Run engineering project planner")
    parser.add_argument("--recursion-limit", "-r", type=int, default=100,
                        help="Recursion limit for processing (default: 100)")
    parser.add_argument("--new", action="store_true",
                        help="Force a fresh build even if a project already exists")

    args = parser.parse_args()

    # Auto-detect: an existing project means a follow-up = update mode.
    # --new forces the build pipeline regardless.
    mode = "update" if (project_has_files() and not args.new) else "build"

    try:
        if mode == "update":
            print("Existing project detected -> UPDATE mode "
                  "(use --new to rebuild from scratch).")
            user_prompt = input("What change would you like to make? ")
        else:
            user_prompt = input("Enter your project prompt: ")

        result = agent.invoke(
            {"user_prompt": user_prompt, "mode": mode},
            {"recursion_limit": args.recursion_limit}
        )
        print("Final State:", result)
    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        sys.exit(0)
    except Exception as e:
        traceback.print_exc()
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()