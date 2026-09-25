#!/usr/bin/env python3
"""
AUSTRO AI - Production Configuration Tests

Run each test in isolation using subprocess to avoid environment pollution.
"""

import subprocess
import sys
import os


def run_test(test_name: str, env_vars: dict, env_code: str, expected_exit: int = 0, expected_output: str = None, not_expected_output: str = None) -> bool:
    """Run a test in a subprocess with given environment."""
    # Build command
    cwd = os.getcwd().replace('\\', '/')
    test_script = f"""
import os
import sys
sys.path.insert(0, '{cwd}')
{env_code}
"""
    # Set environment variables in the subprocess
    env = os.environ.copy()
    env.update(env_vars)
    
    result = subprocess.run(
        [sys.executable, "-c", test_script],
        env=env,
        capture_output=True,
        text=True,
        timeout=30
    )
    
    if result.returncode != expected_exit:
        print(f"FAIL {test_name}: exit code {result.returncode} (expected {expected_exit})")
        print(f"  stdout: {result.stdout}")
        print(f"  stderr: {result.stderr}")
        return False
    
    # Check both stdout and stderr for expected output
    combined_output = result.stdout + result.stderr
    if expected_output and expected_output not in combined_output:
        print(f"FAIL {test_name}: expected output '{expected_output}' not found")
        print(f"  stdout: {result.stdout}")
        print(f"  stderr: {result.stderr}")
        return False
    
    if not_expected_output and not_expected_output in combined_output:
        print(f"FAIL {test_name}: unexpected output '{not_expected_output}' found")
        return False
    
    print(f"PASS {test_name}")
    return True


# Test cases
TESTS = [
    {
        "name": "Development config",
        "env": {
            "BOT_TOKEN": "123456789:ABCdefghIJKlmnoPQRstuvwxyz-DummyToken",
            "GEMINI_API_KEY": "",
            "AUSTRO_ENVIRONMENT": "development",
            "AUSTRO_LOG_LEVEL": "DEBUG",
            "USE_LOCAL_FALLBACK": "true",
        },
        "code": """
from app.config.settings import load_settings
s = load_settings()
assert s.environment == "development"
assert s.log_level == "DEBUG"
assert s.use_local_fallback is True
assert s.has_gemini_key is False
print("PASS")
""",
    },
    {
        "name": "Production config valid",
        "env": {
            "BOT_TOKEN": "123456789:ABCdefghIJKlmnoPQRstuvwxyz-DummyToken",
            "GEMINI_API_KEY": "real-gemini-api-key-here",
            "AUSTRO_ENVIRONMENT": "production",
            "AUSTRO_LOG_LEVEL": "WARNING",
            "USE_LOCAL_FALLBACK": "false",
        },
        "code": """
from app.config.settings import load_settings
s = load_settings()
assert s.environment == "production"
assert s.log_level == "WARNING"
assert s.use_local_fallback is False
assert s.has_gemini_key is True
print("PASS")
""",
    },
    {
        "name": "Production config invalid log_level",
        "env": {
            "BOT_TOKEN": "123456789:ABCdefghIJKlmnoPQRstuvwxyz-DummyToken",
            "GEMINI_API_KEY": "real-gemini-api-key-here",
            "AUSTRO_ENVIRONMENT": "production",
            "AUSTRO_LOG_LEVEL": "INFO",
            "USE_LOCAL_FALLBACK": "false",
        },
        "expected_exit": 1,
        "expected_output": "production environment requires log_level WARNING or ERROR",
        "code": """
from app.config.settings import load_settings
s = load_settings()
""",
    },
    {
        "name": "Missing BOT_TOKEN",
        "env": {
            "BOT_TOKEN": "",
            "GEMINI_API_KEY": "",
            "AUSTRO_ENVIRONMENT": "development",
        },
        "expected_exit": 1,
        "expected_output": "BOT_TOKEN not set",
        "code": """
from app.config.settings import load_settings
s = load_settings()
""",
    },
    {
        "name": "Placeholder BOT_TOKEN",
        "env": {
            "BOT_TOKEN": "your_bot_token_here",
            "GEMINI_API_KEY": "",
            "AUSTRO_ENVIRONMENT": "development",
        },
        "expected_exit": 1,
        "expected_output": "BOT_TOKEN not set",
        "code": """
from app.config.settings import load_settings
s = load_settings()
""",
    },
    {
        "name": "Invalid log_level",
        "env": {
            "BOT_TOKEN": "123456789:ABCdefghIJKlmnoPQRstuvwxyz-DummyToken",
            "GEMINI_API_KEY": "",
            "AUSTRO_ENVIRONMENT": "development",
            "AUSTRO_LOG_LEVEL": "INVALID",
        },
        "expected_exit": 1,
        "expected_output": "Invalid log_level",
        "code": """
from app.config.settings import load_settings
s = load_settings()
""",
    },
    {
        "name": "Defaults",
        "env": {
            "BOT_TOKEN": "123456789:ABCdefghIJKlmnoPQRstuvwxyz-DummyToken",
            "GEMINI_API_KEY": "",
            "AUSTRO_ENVIRONMENT": "development",
        },
        "code": """
from app.config.settings import load_settings
s = load_settings()
assert s.gemini_model == "gemini-2.5-flash"
assert s.max_requests_per_day == 1500
assert s.knowledge_chunk_size == 900
assert s.memory_max_retrieved == 12
assert s.learning_default_session_minutes == 25
print("PASS")
""",
    },
    {
        "name": "Settings immutability",
        "env": {
            "BOT_TOKEN": "123456789:ABCdefghIJKlmnoPQRstuvwxyz-DummyToken",
            "GEMINI_API_KEY": "",
            "AUSTRO_ENVIRONMENT": "development",
        },
        "code": """
from app.config.settings import load_settings
s = load_settings()
try:
    s.bot_token = "changed"
    print("FAIL: Settings should be frozen")
    sys.exit(1)
except Exception:
    print("PASS")
""",
    },
]


def main():
    print("Running Production Configuration Tests...")
    print("=" * 60)
    
    all_passed = True
    for test in TESTS:
        env_code = test["code"]
        env = test["env"]
        expected_exit = test.get("expected_exit", 0)
        expected_output = test.get("expected_output", None)
        
        success = run_test(
            test["name"],
            env,
            env_code,
            expected_exit=expected_exit,
            expected_output=expected_output
        )
        if not success:
            all_passed = False
    
    print("=" * 60)
    if all_passed:
        print("All production configuration tests passed!")
        return 0
    else:
        print("Some tests failed!")
        return 1


if __name__ == "__main__":
    sys.exit(main())