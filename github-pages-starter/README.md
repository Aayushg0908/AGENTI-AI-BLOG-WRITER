# GitHub Pages starter for AgentWriter AI

A minimal Jekyll site (minima theme) that AgentWriter publishes into.

1. Create a **public** GitHub repo named `<your-username>.github.io` (for example `jane.github.io`).
2. Copy the files in this folder into it and push.
3. In the repo: **Settings → Pages → Build and deployment → Source: Deploy from a branch**, branch `main`, folder `/ (root)`.
4. Your blog will be at `https://<your-username>.github.io`.

AgentWriter commits each article to `_posts/YYYY-MM-DD-<slug>.md` (or `_drafts/` for drafts),
served at `/posts/<slug>/`. Mermaid diagrams are rendered in the browser.

## Connecting it in AgentWriter

In the **Publish** card, enter:

- **Repository:** `<your-username>/<your-username>.github.io`
- **Access token:** GitHub → Settings → Developer settings → Fine-grained tokens → Generate.
  Repository access: *Only select repositories* → this repo. Permissions: *Contents: Read and write*.

The token is sent only with each publish request and is not stored on the server.
Tick "Remember on this device" to keep it in your own browser.
