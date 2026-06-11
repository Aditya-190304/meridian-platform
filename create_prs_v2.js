/**
 * Resumes from PR 4 onwards (PRs 1-3 already done or partially done).
 * PR 1 branch is pushed but PR was not created (shell parsing issue with & in body).
 * PR 4 branch had push protection block — fixed, now needs amend + push + PR create.
 * PRs 5-8 need full creation.
 */
const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const OUTPUT_FILE = 'C:\\Users\\Dell\\AppData\\Local\\Temp\\claude\\c--Users-Dell-Desktop-approach-2\\98f9592f-140c-48ea-9fc1-7a8dcbfa4a44\\tasks\\w9k4brr60.output';
const REPO_DIR = 'C:\\Users\\Dell\\Desktop\\pr-eval-repo';
const BODY_TMP = path.join(REPO_DIR, '.pr_body_tmp.md');

function run(cmd, opts = {}) {
  console.log(`  $ ${cmd.slice(0, 120)}`);
  return execSync(cmd, { cwd: REPO_DIR, encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'], ...opts });
}

function decodeHtmlEntities(str) {
  return str
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, ' ');
}

function ensureDir(filePath) {
  const dir = path.dirname(filePath);
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }
}

function createPR(branch, title, body) {
  // Write body to temp file to avoid shell escaping issues
  fs.writeFileSync(BODY_TMP, body, 'utf8');
  const safeTitle = title.replace(/"/g, '\\"');
  const result = run(`gh pr create --title "${safeTitle}" --body-file "${BODY_TMP}" --base main`);
  fs.unlinkSync(BODY_TMP);
  return result.trim();
}

async function main() {
  const raw = fs.readFileSync(OUTPUT_FILE, 'utf8');
  const obj = JSON.parse(raw);
  const prs = obj.result;

  // --- Fix PR 1: branch already pushed, just need to create the PR ---
  console.log('\n=== Fix PR 1: create PR (branch already pushed) ===');
  const pr1 = prs[0];
  try {
    run(`git checkout ${pr1.branch}`);
    const url = createPR(pr1.branch, pr1.pr_title, decodeHtmlEntities(pr1.pr_body));
    console.log(`  PR 1 created: ${url}`);
  } catch (e) {
    console.error(`  PR 1 failed: ${e.message.slice(0, 200)}`);
  }

  // --- Fix PR 4: amend commit (secret replaced), then push ---
  console.log('\n=== Fix PR 4: amend commit and push ===');
  const pr4 = prs[3];
  try {
    run(`git checkout ${pr4.branch}`);
    run('git add app/notifications/adapters.py');
    run('git commit --amend --no-edit');
    run(`git push --force-with-lease origin ${pr4.branch}`);
    const url = createPR(pr4.branch, pr4.pr_title, decodeHtmlEntities(pr4.pr_body));
    console.log(`  PR 4 created: ${url}`);
  } catch (e) {
    console.error(`  PR 4 failed: ${e.message.slice(0, 300)}`);
  }

  // --- PRs 5-8: full creation ---
  for (let i = 4; i < prs.length; i++) {
    const pr = prs[i];
    console.log(`\n=== PR ${i + 1}/8: ${pr.branch} ===`);
    try {
      run('git checkout main');
      run(`git checkout -b ${pr.branch}`);

      for (const file of pr.files) {
        const fullPath = path.join(REPO_DIR, file.path);
        ensureDir(fullPath);
        fs.writeFileSync(fullPath, decodeHtmlEntities(file.content), 'utf8');
        console.log(`  wrote: ${file.path}`);
      }

      run('git add -A');
      const safeMsg = pr.pr_title.replace(/"/g, '\\"');
      run(`git commit -m "${safeMsg}"`);
      run(`git push -u origin ${pr.branch}`);

      const url = createPR(pr.branch, pr.pr_title, decodeHtmlEntities(pr.pr_body));
      console.log(`  PR created: ${url}`);
    } catch (e) {
      console.error(`  Failed PR ${i + 1}: ${e.message.slice(0, 300)}`);
    }
  }

  console.log('\n=== Done ===');
}

main().catch(console.error);
