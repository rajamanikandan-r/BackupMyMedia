# BackupMyMedia

A Flask web app for backing up and browsing photos on Google Cloud (GCS + Firestore).

**Live URL:** https://gallery-service-89549779745.us-central1.run.app

---

## Run Locally

```bash
source .venv/bin/activate
python main.py
```

Open http://localhost:8080

---

## Deploy to Google Cloud

### First-time setup

```bash
# Enable required APIs
gcloud services enable secretmanager.googleapis.com --project=life-begins-at-40

# Upload service account key to Secret Manager
gcloud secrets create gcp-service-account-key \
  --data-file=life-begins-at-40-a0cf724dc4fe.json \
  --project=life-begins-at-40

# Grant Cloud Run access to the secret
gcloud secrets add-iam-policy-binding gcp-service-account-key \
  --member="serviceAccount:89549779745-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" \
  --project=life-begins-at-40
```

### Build and deploy

```bash
# Build image
gcloud builds submit \
  --tag us-central1-docker.pkg.dev/life-begins-at-40/cloud-run-source-deploy/gallery-service \
  --project=life-begins-at-40

# Deploy
gcloud run deploy gallery-service \
  --image us-central1-docker.pkg.dev/life-begins-at-40/cloud-run-source-deploy/gallery-service \
  --region us-central1 \
  --project life-begins-at-40 \
  --update-secrets=/secrets/key.json=gcp-service-account-key:latest \
  --set-env-vars KEY_PATH=/secrets/key.json
```
