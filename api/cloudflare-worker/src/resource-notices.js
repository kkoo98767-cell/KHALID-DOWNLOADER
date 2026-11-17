export async function sendBackgroundNotices(env, storage) {
  if (!env.RESOURCE_NOTICE_WORKFLOW || !env.GITHUB_TOKEN || !env.GITHUB_OWNER || !env.GITHUB_REPO) return true;
  const { sql } = storage;
  const read = name => JSON.parse(sql.exec("SELECT value FROM resource_meta WHERE name=?", name).toArray()[0]?.value || "[]");
  const write = (name, value) => sql.exec("INSERT OR REPLACE INTO resource_meta VALUES (?,?)", name, JSON.stringify(value));
  const batch = storage.transactionSync(() => {
    const pending = [...new Set([...read("notices_sending"), ...read("notices")])].slice(-12);
    if (pending.length) {
      write("notices_sending", pending);
      write("notices", []);
    }
    return pending;
  });
  if (!batch.length) return true;

  try {
    const repository = encodeURIComponent(env.GITHUB_OWNER) + "/" + encodeURIComponent(env.GITHUB_REPO);
    const workflow = encodeURIComponent(env.RESOURCE_NOTICE_WORKFLOW);
    const response = await fetch(`https://api.github.com/repos/${repository}/actions/workflows/${workflow}/dispatches`, {
      method: "POST",
      headers: {
        accept: "application/vnd.github+json",
        authorization: "Bearer " + env.GITHUB_TOKEN,
        "content-type": "application/json",
        "user-agent": "khalid-downloader-worker",
        "x-github-api-version": "2022-11-28",
      },
      body: JSON.stringify({ ref: "main", inputs: { notice: batch.join("\n\n").slice(0, 3000) } }),
      redirect: "manual",
      signal: AbortSignal.timeout(5000),
    });
    if (response.status === 204 || response.status === 200) {
      write("notices_sending", []);
      return true;
    }
  } catch {
    console.warn("Resource alert dispatch unavailable");
  }
  storage.transactionSync(() => {
    write("notices", [...new Set([...batch, ...read("notices")])].slice(-12));
    write("notices_sending", []);
  });
  return false;
}
