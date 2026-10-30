# smartthings-evo
SmartThings is 'evolving' their developer API by charging for access starting in October. smartthings-evo is a copy of the ha-core smartthings component, refactored to mirror the authentication flow of the android mobile app, requiring a browser extension to capture the sasdk:// redirect uri and finish authorization.

## Push vs Polling
This login method can't subscribe to push updates, so the integration now polls the REST api every 30s.

# Authentication

> [!IMPORTANT]
> A browser extension is required to successfully authenticate with SmartThings (capture the sasdk:// URI). Do not skip this step!

SmartThings mobile uses a Samsung account login flow that blocks automated logins. Authentication requires a browser-based OAuth flow using a browser extension to capture the mobile app redirect URI.

## Setup

   ### chrome-extension
   > The chrome extension is tied to the folder location on your computer and may disappear if you move the folder.
   - Download the project 
   - Extract the zip file and locate `./extensions/chrome-extension/`
   - Open Google Chrome and navigate to `chrome://extensions/` or Edge `edge://extensions/`
   - Enable "Developer mode"
   - Click "Load unpacked" and select the extracted `extensions/chrome-extension` folder
   - Start the automated authentication flow in HA by the integration
       - If your browser still has a Samsung login cookie, the login is skipped and no redirect URI is produced to capture — open the extension pop-up, reset Samsung auth to delete existing cookies, return to HA, and click `open website` to continue authentication
   - After logging in to Samsung the redirect should be captured and re-written to my-homeassistant.io
   - Proceed with the `Link to Home Assistant` process

   ### Known Issue
   - A tab loads a 500 internal server error but authentication proceeds in HA

# Original codeowner
Thanks to joostlek for handling the integration thus far
