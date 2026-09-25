#!/usr/bin/env python3
"""
AUSTRO AI - Dependency Security Audit Script

Runs pip-audit on project dependencies and reports vulnerabilities.
"""

import json
import subprocess
import sys
from pathlib import Path


def run_audit():
    """Run pip-audit and return results."""
    repo_root = Path(__file__).parent.parent
    req_files = [
        repo_root / "requirements.txt",
        repo_root / "requirements-dev.txt",
    ]
    
    cmd = ["pip-audit"]
    for req in req_files:
        if req.exists():
            cmd.extend(["-r", str(req)])
    cmd.extend(["--format=json"])
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "pip-audit timed out"
    except FileNotFoundError:
        return -1, "", "pip-audit not installed"


def main():
    print("Running dependency security audit...")
    returncode, stdout, stderr = run_audit()
    
    if returncode == -1:
        print(f"ERROR: {stderr}")
        print("TOOL BLOCKED")
        return 2
    
    if stdout.strip():
        try:
            audit_data = json.loads(stdout)
            vulns_found = False
            for dep in audit_data.get("dependencies", []):
                for vuln in dep.get("vulns", []):
                    vulns_found = True
                    print(f"VULNERABILITY: {dep['name']}=={dep['version']}")
                    print(f"  ID: {vuln['id']}")
                    print(f"  Fix: {vuln.get('fix_versions', ['unknown'])}")
                    print(f"  Description: {vuln.get('description', '')[:200]}...")
                    print()
            
            if vulns_found:
                print("VULNERABILITIES FOUND")
                return 1
            else:
                print("PASS - No known vulnerabilities")
                return 0
        except json.JSONDecodeError:
            print(f"ERROR: Failed to parse pip-audit output: {stdout[:500]}")
            return 2
    else:
        print("No output from pip-audit")
        if stderr:
            print(f"stderr: {stderr}")
        return 2


if __name__ == "__main__":
    sys.exit(main())