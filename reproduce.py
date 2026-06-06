import shutil
import sys
from pathlib import Path

import pexpect


def main():
    session_name = "test-e2e-resume"
    print(f"Bootstrapping Hatchery task '{session_name}' programmatically...")

    # We must run this bootstrap step in the python context of seekr-hatchery
    repo = Path("/home/cam/repos/seekr-hatchery")

    # 1. Clean up any previous runs
    task_dir = Path("/home/cam/.hatchery/tasks/seekr-hatchery-f525dea9") / session_name
    if task_dir.exists():
        print(f"Removing existing task metadata directory: {task_dir}")
        shutil.rmtree(task_dir)

    task_file_in_repo = repo / ".hatchery" / "tasks" / f"{session_name}.md"
    if task_file_in_repo.exists():
        task_file_in_repo.unlink()

    # Clear previous e2e file
    test_file = repo / "hello_hatchery.txt"
    if test_file.exists():
        test_file.unlink()

    # 2. Programmatically create the task metadata and task file
    import seekr_hatchery.agents as agents
    import seekr_hatchery.sessions as sessions

    meta = sessions.create(
        name=session_name,
        repo=repo,
        type="task",
        backend=agents.ANTIGRAVITY,
        no_worktree=True,  # Running in repo directory to easily check file creation
        no_commit=True,
        no_commit_docker=True,
        objective="Create a file named 'hello_hatchery.txt' in the current workspace directory (/workspace) containing exactly the text 'HATCHERY_E2E_TEST_SUCCESSFUL'. Do it now, do not ask for any confirmation, and let me know when done.",
        use_editor=False,
    )
    print(f"Task bootstrapped successfully! session_id: {meta.session_id}")

    # 3. Spawn hatchery resume command
    print(f"Spawning hatchery resume for '{session_name}'...")
    child = pexpect.spawn(
        f"/home/cam/.local/bin/uv run hatchery --log-level DEBUG resume {session_name}",
        encoding="utf-8",
        timeout=180,
    )

    # Log child output to stdout
    child.logfile = sys.stdout

    try:
        # Wait for the post-exit prompt
        print("\n--- Waiting for task execution to complete and show post-exit prompt ---")
        child.expect("Mark task 'test-e2e-resume' as done", timeout=120)

        # Send 'y' to mark done and complete
        print("\n--- Sending 'y' to mark task complete ---")
        child.sendline("y")

        child.expect(pexpect.EOF, timeout=10)
        print("\n--- Hatchery session exited successfully! ---")
    except Exception as e:
        print(f"\nException occurred: {e}")
    finally:
        child.close()

    # 4. Assert file contents
    print("\n--- Verifying file contents ---")
    if test_file.exists():
        content = test_file.read_text().strip()
        print(f"File found! Content: '{content}'")
        if content == "HATCHERY_E2E_TEST_SUCCESSFUL":
            print("\nSUCCESS: E2E Hatchery validation completed successfully!")
            sys.exit(0)
        else:
            print("\nFAILURE: File content does not match!")
            sys.exit(1)
    else:
        print("\nFAILURE: File hello_hatchery.txt was not created!")
        sys.exit(1)


if __name__ == "__main__":
    main()
