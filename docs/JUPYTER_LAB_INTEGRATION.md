# JupyterLab / code-execution integration

## Recommendation

Use Jupyter locally for the first learning and experimentation workflows. When the app needs to offer a browser-based lab, connect it to a **separate Jupyter Server or JupyterHub service**. Do not embed a kernel inside the web API process and do not expose an unauthenticated Jupyter endpoint to the public internet.

Jupyter Server provides REST endpoints for resources such as contents and sessions, plus WebSocket-based kernel channels for execution and output streaming. Its security documentation explicitly notes that access to a Jupyter Server means the ability to run arbitrary code and describes token-based authentication as enabled by default:

- REST API: <https://jupyter-server.readthedocs.io/en/stable/developers/rest-api.html>
- Security guide: <https://jupyter-server.readthedocs.io/en/latest/operators/security.html>
- JupyterHub: <https://jupyterhub.readthedocs.io/en/stable/>

## Target architecture

```text
Browser / learning app
        │ authenticated app session
        ▼
App backend (authorizes user, creates lab session)
        │ service-to-service auth; never expose server token to browser
        ▼
Lab gateway / JupyterHub
        │ per-user server or isolated container
        ▼
Ephemeral kernel with CPU, RAM, disk, process, and time limits
```

## Integration stages

### Stage A — local lab (now)

- Use `notebooks/00_inventory_exploration.ipynb` for exploratory work against the local seed manifest.
- Install optional dependencies with `python -m pip install -r requirements-notebooks.txt`.
- Start `jupyter lab` locally. It is for the project owner, not a public multi-user code execution endpoint.

### Stage B — private single-user app integration

- Run Jupyter Server separately from the app and keep it behind a reverse proxy or private network.
- Authenticate the app user before creating or attaching to a notebook session.
- Keep the Jupyter token on the backend; do not place it in frontend JavaScript, URLs visible to other users, logs, or public configuration.
- Use an allow-list of notebook templates and workspace paths.
- Enforce kernel lifetime and execution limits at the service/container level.

### Stage C — multiple users

Use JupyterHub or an equivalent per-user execution orchestrator rather than sharing one powerful kernel across all users. Each user's session should have separate identity, filesystem, and resource boundaries.

## Minimum security controls

Code submitted to a notebook can read/write files, consume CPU and memory, spawn processes, and make network requests if the kernel allows it. Apply defense in depth:

- Separate container or sandbox for each user/session; run as non-root.
- CPU, memory, disk, process-count, and wall-clock limits.
- No host mounts or access to application secrets.
- Do not mount production databases or source credentials into the kernel.
- Default-deny outbound network access, with narrowly approved exceptions if needed.
- Authentication, authorization, CSRF/origin protections, and TLS at the gateway.
- Automatic idle shutdown and deletion of disposable sessions.
- Auditing without logging secrets or notebook outputs that may contain private data.
- Patch Jupyter, Python packages, and base images; scan packages where appropriate.

A notebook kernel is not a security sandbox by itself. Containers are a starting boundary, not a guarantee against every class of attack.

## API interaction shape (conceptual)

The app backend will need to manage a session lifecycle, read/write notebook files, and connect to a kernel's WebSocket channels. Implement against the current Jupyter Server/JupyterHub version and its API documentation rather than hard-coding an undocumented API. Add per-user authorization checks to every lab/session identifier.

Do not implement public code execution until authentication, resource limits, network policy, and isolation have been tested.
