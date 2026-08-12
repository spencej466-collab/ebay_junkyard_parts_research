# Junkyard Parts Research

Streamlit prototype for researching live eBay used automotive-part listings with the eBay Browse API.

## Local run

PowerShell:

```powershell
$env:EBAY_CLIENT_ID="YOUR_PRODUCTION_CLIENT_ID"
$env:EBAY_CLIENT_SECRET="YOUR_PRODUCTION_CLIENT_SECRET"
python -m streamlit run app.py
```

## Streamlit Community Cloud

Deploy the repository from GitHub and configure the secrets in the app's Advanced settings:

```toml
EBAY_CLIENT_ID = "YOUR_PRODUCTION_CLIENT_ID"
EBAY_CLIENT_SECRET = "YOUR_PRODUCTION_CLIENT_SECRET"
```

Do not commit `secrets.toml` or credentials to GitHub.

The application currently uses the public Browse API for active listing research. The Marketplace Insights API is intentionally not hard-coded into the app because that access is restricted and must be approved by eBay.
