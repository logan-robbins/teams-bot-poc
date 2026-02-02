# TASK: Deploy Python Agent to Azure with Azure OpenAI

## Overview
Deploy the FastAPI transcript sink and Streamlit UI to Azure Container Apps with Azure OpenAI backend.

**Target Subdomain:** `agent.qmachina.com`  
**Current Status:** Ready to Deploy

---

## Plan

### 1. Azure Infrastructure [complete]
- [x] Create Azure Container Apps Environment (minimal POC size)
- [x] Deploy Azure OpenAI resource with gpt-4o model
- [x] Deploy FastAPI container (port 8765)
- [x] Deploy Streamlit container (port 8501)
- [x] Configure ingress with custom domain support
- Script: `scripts/deploy-azure-agent.sh`

### 2. Code Updates for Azure OpenAI [complete]
- [x] Update interview_agent/agent.py to support Azure OpenAI
- [x] Add Azure OpenAI configuration documentation to README
- [x] Update streamlit_ui.py to use SINK_URL environment variable
- [x] Create Dockerfile for containerization

### 3. GoDaddy DNS Setup [documented]
- [x] Document CNAME record creation (see README.md)
- [ ] Execute after running deploy script (manual step)

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
```

### Manual DNS Steps (after deploy)
1. Go to GoDaddy DNS: https://dcc.godaddy.com/manage/dns
2. Create CNAME record: `agent` → `<fastapi-fqdn-from-script-output>`
3. Wait 5-15 minutes for propagation
4. Run hostname bind commands (output by script)
