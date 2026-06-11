const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const OUTPUT_FILE = 'C:\\Users\\Dell\\AppData\\Local\\Temp\\claude\\c--Users-Dell-Desktop-approach-2\\98f9592f-140c-48ea-9fc1-7a8dcbfa4a44\\tasks\\w9k4brr60.output';
const REPO_DIR = 'C:\\Users\\Dell\\Desktop\\pr-eval-repo';

function run(cmd, opts = {}) {
  console.log(`  $ ${cmd}`);
  return execSync(cmd, { cwd: REPO_DIR, encoding: 'utf8', ...opts });
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

async function main() {
  const raw = fs.readFileSync(OUTPUT_FILE, 'utf8');
  const obj = JSON.parse(raw);
  const prs = obj.result;

  console.log(`Processing ${prs.length} PRs...\n`);

  for (let i = 0; i < prs.length; i++) {
    const pr = prs[i];
    console.log(`\n=== PR ${i + 1}/${prs.length}: ${pr.branch} ===`);

    // Checkout main and create new branch
    run('git checkout main');
    run(`git checkout -b ${pr.branch}`);

    // Write all files
    for (const file of pr.files) {
      const fullPath = path.join(REPO_DIR, file.path);
      ensureDir(fullPath);
      const content = decodeHtmlEntities(file.content);
      fs.writeFileSync(fullPath, content, 'utf8');
      console.log(`  wrote: ${file.path}`);
    }

    // Stage and commit
    run('git add -A');
    const commitMsg = pr.pr_title.replace(/"/g, '\\"');
    run(`git commit -m "${commitMsg}"`);

    // Push branch
    run(`git push -u origin ${pr.branch}`);

    // Create PR
    const body = decodeHtmlEntities(pr.pr_body).replace(/"/g, '\\"').replace(/\n/g, '\\n');
    try {
      const prUrl = run(
        `gh pr create --title "${pr.pr_title.replace(/"/g, '\\"')}" --body "${body}" --base main`
      );
      console.log(`  PR created: ${prUrl.trim()}`);
    } catch (e) {
      console.error(`  PR create failed: ${e.message}`);
    }

    console.log(`  Done PR ${i + 1}`);
  }

  console.log('\n=== All PRs created ===');
}

main().catch(console.error);
