const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const OUTPUT_FILE = 'C:\\Users\\Dell\\AppData\\Local\\Temp\\claude\\c--Users-Dell-Desktop-approach-2\\98f9592f-140c-48ea-9fc1-7a8dcbfa4a44\\tasks\\w1gtnlu8n.output';
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
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
}

function createPR(title, body) {
  fs.writeFileSync(BODY_TMP, body, 'utf8');
  const safeTitle = title.replace(/"/g, '\\"');
  const result = run(`gh pr create --title "${safeTitle}" --body-file "${BODY_TMP}" --base main`);
  try { fs.unlinkSync(BODY_TMP); } catch {}
  return result.trim();
}

async function main() {
  const raw = fs.readFileSync(OUTPUT_FILE, 'utf8');
  const obj = JSON.parse(raw);
  const prs = obj.result;

  console.log(`Processing ${prs.length} PRs...\n`);

  for (let i = 0; i < prs.length; i++) {
    const pr = prs[i];
    console.log(`\n=== PR ${i + 1}/${prs.length}: ${pr.branch} ===`);

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

      const url = createPR(pr.pr_title, decodeHtmlEntities(pr.pr_body));
      console.log(`  PR created: ${url}`);
    } catch (e) {
      console.error(`  FAILED PR ${i + 1}: ${e.message.slice(0, 400)}`);
      // Try to get back to main for next iteration
      try { run('git checkout main'); } catch {}
    }
  }

  console.log('\n=== Phase 2 done ===');
}

main().catch(console.error);
