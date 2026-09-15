#!/usr/bin/env python3
"""Automated judge scorer — evaluates benchmark responses using a judge model."""

import json
import os
import re
import sqlite3
import subprocess
import tempfile
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), 'benchmark-results.db')
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), 'schema.sql')
RUBRIC_PATH = os.path.join(os.path.dirname(__file__), 'rubric_anchors.md')
CORPUS_PATH = os.path.join(os.path.dirname(__file__), 'corpus.json')

JUDGE_MODEL_PORT = 8082  # 3070
JUDGE_MODEL_ID = "qwen35-9b"  # For display only

# Judge scoring prompt template
JUDGE_PROMPT_TEMPLATE = """You are a code quality judge. Score the following response on {num_dimensions} dimensions.

## Task
{task_prompt}

## Expected Behavior
{expected_behavior}

## Model Response
{response}

## Scoring Rubric
{rubric}

## Instructions
Score each dimension INDEPENDENTLY. For each dimension:
1. Read the rubric anchor that best matches the response
2. Assign a score from 0 to 10
3. Write a 1-sentence justification referencing specific evidence from the response

Respond in this EXACT JSON format:
{{
  "scores": {{
    "completeness": {{"score": N, "justification": "..."}},
    "quality": {{"score": N, "justification": "..."}},
    "intelligence": {{"score": N, "justification": "..."}},
    "role_fit": {{"score": N, "justification": "..."}}
  }}
}}

Do NOT include "correctness" in your scores — that is measured by execution, not judgment.
"""


class JudgeScorer:
    """Scores benchmark responses using a judge model."""

    def __init__(self, db_path=None, ssh_client=None):
        self.db_path = db_path or DB_PATH
        self.ssh = ssh_client
        self.rubric = self._load_rubric()
        self.corpus = self._load_corpus()
        self._init_db()

    def _load_rubric(self):
        with open(RUBRIC_PATH) as f:
            return f.read()

    def _load_corpus(self):
        with open(CORPUS_PATH) as f:
            tasks = json.load(f)
        return {t['id']: t for t in tasks}

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        # Guard against duplicate schema_version INSERT on re-init
        with open(SCHEMA_PATH) as f:
            schema_sql = f.read()
        schema_sql = schema_sql.replace(
            "INSERT INTO schema_version (version) VALUES (1);",
            "INSERT OR IGNORE INTO schema_version (version) VALUES (1);",
        )
        conn.executescript(schema_sql)
        conn.close()

    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _strip_blinding(self, response_text):
        """Strip model metadata from response for blinding."""
        lines = response_text.split('\n')
        filtered = []
        for line in lines:
            if any(keyword in line.lower() for keyword in
                   ['model:', 'model name', 'i am', "i'm a", 'powered by',
                    'developed by', 'based on', 'architecture:']):
                continue
            filtered.append(line)
        return '\n'.join(filtered)

    def _execute_code_test(self, response_text, task):
        """Attempt to execute generated code and run tests.

        Returns:
            dict with keys: passed, total, output
        """
        automated_check = task.get('automated_check')
        if not automated_check:
            return {'passed': None, 'total': None, 'output': 'No automated check defined'}

        # Extract code blocks from response
        code_blocks = re.findall(r'```(?:python)?\s*\n(.*?)```', response_text, re.DOTALL)
        if not code_blocks:
            return {'passed': 0, 'total': 1, 'output': 'No code blocks found in response'}

        # Try to run the automated check with the generated code
        try:
            # Write code to temp file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
                full_code = '\n\n'.join(code_blocks)
                f.write(full_code)
                f.write('\n')
                code_path = f.name

            # Run the automated check
            result = subprocess.run(
                automated_check,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=os.path.dirname(code_path)
            )

            os.unlink(code_path)

            if result.returncode == 0:
                return {'passed': 1, 'total': 1, 'output': result.stdout[:500]}
            else:
                return {'passed': 0, 'total': 1, 'output': (result.stdout + result.stderr)[:500]}

        except subprocess.TimeoutExpired:
            return {'passed': 0, 'total': 1, 'output': 'Execution timed out (30s)'}
        except Exception as e:
            return {'passed': 0, 'total': 1, 'output': f'Execution error: {str(e)[:200]}'}

    def score_run(self, run_id, dry_run=False):
        """Score a single benchmark run.

        Args:
            run_id: benchmark_runs.id to score
            dry_run: if True, return scoring plan without executing

        Returns:
            dict with scores or dry_run plan
        """
        conn = self._get_connection()

        # Get run data
        run = conn.execute(
            "SELECT * FROM benchmark_runs WHERE id=?", (run_id,)
        ).fetchone()

        if run is None:
            conn.close()
            raise ValueError(f"Run {run_id} not found")

        if run['status'] != 'complete':
            conn.close()
            raise ValueError(f"Run {run_id} is not complete (status={run['status']})")

        task = self.corpus.get(run['task_id'])
        if task is None:
            conn.close()
            raise ValueError(f"Task {run['task_id']} not found in corpus")

        response_text = run['response_text'] or ''

        # Blind the response
        blinded_response = self._strip_blinding(response_text)

        # Execute automated tests for coder tasks
        execution_result = None
        correctness_score = None
        if task.get('role') == 'coder' and task.get('automated_check'):
            execution_result = self._execute_code_test(response_text, task)
            if execution_result['total'] is not None:
                correctness_score = int((execution_result['passed'] / execution_result['total']) * 10)

        if dry_run:
            conn.close()
            return {
                'run_id': run_id,
                'task_id': run['task_id'],
                'role': task['role'],
                'has_automated_check': task.get('automated_check') is not None,
                'execution_result': execution_result,
                'correctness_score': correctness_score,
                'status': 'would_score'
            }

        # Build judge prompt (skip correctness dimension — it's execution-based)
        dimensions = ['completeness', 'quality', 'intelligence', 'role_fit']
        judge_prompt = JUDGE_PROMPT_TEMPLATE.format(
            num_dimensions=len(dimensions),
            task_prompt=task['prompt'][:1000],  # Truncate long prompts
            expected_behavior=task.get('expected_behavior', 'See task description')[:500],
            response=blinded_response[:2000],  # Truncate long responses
            rubric=self._extract_rubric_section(dimensions)
        )

        # Call judge model
        scores = {}
        try:
            result = self.ssh.curl_beellama(
                port=JUDGE_MODEL_PORT,
                messages=[{"role": "user", "content": judge_prompt}],
                max_tokens=1024,
                temperature=0.1,  # Low temp for consistent scoring
            )

            # Parse judge response
            judge_content = result.get('content', '')
            scores = self._parse_judge_scores(judge_content)

        except Exception as e:
            # If judge fails, record error but continue with correctness
            scores = {}

        # Insert one row with all scores
        completeness = scores.get('completeness', {}).get('score')
        quality = scores.get('quality', {}).get('score')
        intelligence = scores.get('intelligence', {}).get('score')
        role_fit = scores.get('role_fit', {}).get('score')

        # Build reasoning JSON
        reasoning = json.dumps(scores) if scores else f"Judge error or no scores parsed"

        try:
            conn.execute("""
                INSERT OR REPLACE INTO judge_scores
                (run_id, judge_model, completeness, correctness, quality,
                 intelligence, role_fit, judge_reasoning, scored_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                run_id, JUDGE_MODEL_ID,
                completeness,
                correctness_score,
                quality,
                intelligence,
                role_fit,
                reasoning,
                datetime.now().isoformat()
            ))
            conn.commit()
        except Exception as e:
            # If insert fails (e.g. generated column issue), try without overall
            conn.execute("""
                INSERT OR REPLACE INTO judge_scores
                (run_id, judge_model, completeness, correctness, quality,
                 intelligence, role_fit, judge_reasoning, scored_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                run_id, JUDGE_MODEL_ID,
                completeness,
                correctness_score,
                quality,
                intelligence,
                role_fit,
                reasoning,
                datetime.now().isoformat()
            ))
            conn.commit()

        conn.close()
        return {
            'run_id': run_id,
            'scores': scores,
            'correctness': correctness_score,
            'execution_result': execution_result
        }

    def _extract_rubric_section(self, dimensions):
        """Extract relevant rubric sections for the judge prompt."""
        lines = self.rubric.split('\n')
        relevant = []
        capture = False
        for line in lines:
            # Check if this is a section header for one of our dimensions
            is_header = line.startswith('## ')
            if is_header:
                # Check if it matches any dimension
                match = False
                for dim in dimensions:
                    dim_title = dim.replace('_', '-').title()
                    dim_alt = dim.replace('_', ' ').title()
                    if dim_title in line or dim_alt in line:
                        match = True
                        break
                if match:
                    capture = True
                elif capture:
                    capture = False
            if capture:
                relevant.append(line)
        return '\n'.join(relevant[:50])  # Limit rubric length

    def _parse_judge_scores(self, judge_response):
        """Parse JSON scores from judge model response."""
        # Try to find JSON in response
        json_match = re.search(r'\{[\s\S]*"scores"[\s\S]*\}', judge_response)
        if json_match:
            try:
                data = json.loads(json_match.group())
                return data.get('scores', {})
            except (json.JSONDecodeError, AttributeError):
                pass

        # Fallback: try to parse the entire response as JSON
        try:
            data = json.loads(judge_response)
            return data.get('scores', data)
        except json.JSONDecodeError:
            pass

        # Last resort: extract scores with regex
        scores = {}
        for dim in ['completeness', 'quality', 'intelligence', 'role_fit']:
            match = re.search(rf'{dim}["\s:]+(\d+)', judge_response, re.IGNORECASE)
            if match:
                scores[dim] = {'score': int(match.group(1)), 'justification': 'Parsed from response'}

        return scores

    def score_batch(self, run_ids=None, dry_run=False):
        """Score multiple runs.

        Args:
            run_ids: list of run IDs (None = all unscored complete runs)
            dry_run: if True, return plans without executing

        Returns:
            list of scoring results
        """
        conn = self._get_connection()

        if run_ids is None:
            # Find all complete runs that haven't been scored
            rows = conn.execute("""
                SELECT br.id FROM benchmark_runs br
                LEFT JOIN judge_scores js ON br.id = js.run_id
                WHERE br.status = 'complete' AND js.id IS NULL
            """).fetchall()
            run_ids = [r['id'] for r in rows]

        conn.close()

        results = []
        for run_id in run_ids:
            try:
                result = self.score_run(run_id, dry_run=dry_run)
                results.append(result)
                if not dry_run:
                    print(f"  Scored run {run_id}")
            except Exception as e:
                print(f"  Failed to score run {run_id}: {e}")
                results.append({'run_id': run_id, 'error': str(e)})

        return results

    def get_scoring_summary(self):
        """Print summary of all scores in the database."""
        conn = self._get_connection()

        total = conn.execute("SELECT COUNT(*) as c FROM judge_scores").fetchone()['c']
        print(f"\nScoring Summary:")
        print(f"  Total scored runs: {total}")

        if total > 0:
            # Average scores per dimension
            for dim in ['completeness', 'correctness', 'quality', 'intelligence', 'role_fit']:
                avg = conn.execute(
                    f"SELECT AVG({dim}) as a FROM judge_scores WHERE {dim} IS NOT NULL"
                ).fetchone()['a']
                if avg is not None:
                    print(f"  Avg {dim}: {avg:.1f}/10")

        conn.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Judge Scorer')
    parser.add_argument('--run-id', type=int, help='Score a specific run')
    parser.add_argument('--batch', action='store_true', help='Score all unscored runs')
    parser.add_argument('--dry-run', action='store_true', help='Print plan without executing')
    parser.add_argument('--summary', action='store_true', help='Print scoring summary')
    args = parser.parse_args()

    from ssh_utils import SSHClient
    ssh = SSHClient()
    ssh.connect()

    judge = JudgeScorer(ssh_client=ssh)

    if args.summary:
        judge.get_scoring_summary()
    elif args.run_id:
        result = judge.score_run(args.run_id, dry_run=args.dry_run)
        print(f"Result: {result}")
    elif args.batch:
        results = judge.score_batch(dry_run=args.dry_run)
        print(f"\nScored {len(results)} runs")
    else:
        print("Use --run-id, --batch, or --summary")
