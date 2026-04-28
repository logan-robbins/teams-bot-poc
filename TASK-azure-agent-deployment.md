# TASK: Deploy Python Agent to Azure with Azure OpenAI

Current status: **historical deployment task, superseded by the eastus
production topology in `README.md` and `STATE.md`**. The live stack now uses
FastAPI `ca-alfred-api` plus React/nginx `ca-alfred-web` on Azure Container
Apps, Azure OpenAI deployment `gpt-5-mini`, and product logic in
`python/meeting_agent/`.

## Overview
Deploy the FastAPI transcript sink and React UI to Azure Container Apps with Azure OpenAI backend.

**Target Subdomain:** `agent.qmachina.com`  
**Current Status:** Complete in eastus; use `scripts/deploy-azure-agent.sh` to redeploy

---

## Plan

### 1. Azure Infrastructure [complete]
- [x] Create Azure Container Apps Environment (minimal POC size)
- [x] Deploy Azure OpenAI resource with `gpt-5-mini` model
- [x] Deploy FastAPI container (port 8765)
- [x] Deploy React/nginx container (port 80)
- [x] Configure ingress with custom domain support
- Script: `scripts/deploy-azure-agent.sh`

### 2. Code Updates for Azure OpenAI [complete]
- [x] Update `meeting_agent/agent.py` to support Azure OpenAI
- [x] Add Azure OpenAI configuration documentation to README
- [x] Update React/nginx UI to proxy `/sink/*` to the FastAPI sink
- [x] Create Dockerfiles for containerization

### 3. GoDaddy DNS Setup [documented]
- [x] Document CNAME record creation (see README.md)
- [x] Execute after running deploy script for production custom domains

### 4. TLS/SSL [automated]
- [x] Configure managed certificate in Azure Container Apps (via az containerapp hostname bind)

---

## Commands Reference

### Deploy Script Location
```bash
./scripts/deploy-azure-agent.sh
```

### Verify Deployment
```bash
curl https://agent.qmachina.com/health
curl https://agent.qmachina.com/stats
curl -sS -o /dev/null -w "%{http_code}\n" https://alfred.qmachina.com
```

### Manual DNS Steps (after deploy)
1. Go to GoDaddy DNS: https://dcc.godaddy.com/manage/dns
2. Create CNAME record: `agent` → `<fastapi-fqdn-from-script-output>`
3. Create CNAME record: `alfred` → `<web-fqdn-from-script-output>`
4. Wait 5-15 minutes for propagation
5. Run hostname bind commands (output by script)
