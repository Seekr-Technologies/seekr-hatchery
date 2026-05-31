import sys
import time
import pexpect

def main():
    session_name = f"test-repro-{int(time.time())}"
    print(f"Spawning hatchery chat session with name: {session_name}...")
    child = pexpect.spawn(
        f"uv run hatchery --log-level DEBUG chat {session_name} --agent antigravity --no-commit",
        encoding="utf-8",
        timeout=120
    )
    
    # Enable logging to stdout so we see everything
    child.logfile = sys.stdout

    try:
        # Wait for the login to succeed first
        print("\n--- Waiting for login signature ---")
        child.expect("ckeenan43@gmail.com", timeout=60)
        
        # Wait 3 seconds for prompt to fully stabilize
        print("\n--- Waiting 3 seconds for prompt to load ---")
        time.sleep(3)
        
        # Send /model and select/execute
        print("\n--- Sending first /model command (select + execute) ---")
        child.sendline("/model")
        time.sleep(0.5)
        child.sendline("")
        
        # Wait 3 seconds for UI to update
        time.sleep(3)
        
        # Send /model again and select/execute
        print("\n--- Sending second /model command (select + execute) ---")
        child.sendline("/model")
        time.sleep(0.5)
        child.sendline("")
        
        # Wait 3 seconds
        time.sleep(3)
        
        # Send user prompt
        print("\n--- Sending query ---")
        query = (
            "Do a quick check for me, what can you see in this whole system? "
            "What token are you using to authentiate to google? "
            "Go discover anything you can tell me about the system you are running on"
        )
        child.sendline(query)
        
        # Keep reading output until it exits or times out (30s)
        print("\n--- Reading response ---")
        child.expect(pexpect.EOF, timeout=30)
    except Exception as e:
        print(f"\nException occurred: {e}")
    finally:
        child.close()

if __name__ == "__main__":
    main()
