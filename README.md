# smartthings-evo
SmartThings is 'evolving' their developer API by charging for access starting in October. smartthings-evo is a copy of the ha-core smartthings component, refactored to mirror the authentication flow of the android mobile app, requiring a browser extension to capture the sasdk:// redirect uri and finish authorization.

## Push vs Polling
This login method can't subscribe to push updates, so the integration now polls the REST api every 30s.

## AI Transparency
Within the last year I've more heavily used AI to work on various home assistant integrations. I'm a code-savvy, python-literate amateur who knows enough to spot when Claude is being a complete idiot, writing some convoluted insanity, or just making bad judgment calls. I review the code committed, excluding tests, where I only guide it to keep them practical instead of exhaustive. I try to add a human touch to the release notes for changes or features I considered, investigated, tested, captured traffic for, and spent time on. I let AI write some of the more tedious fix notes and backports from other forks, making sure appropriate credit is added. AI is not where I started, but I wouldn't have had the time to work on this project without it or likely been able to solve the auth.

# Authentication

> [!IMPORTANT]
> A browser extension is required to successfully authenticate with SmartThings (capture the sasdk:// URI). Do not skip this step!

SmartThings mobile uses a Samsung account login flow that blocks automated logins. Authentication requires a browser-based OAuth flow using a browser extension to capture the mobile app redirect URI.

## Setup

Before starting, make sure Home Assistant Cloud is enabled and install the
unpacked browser extension from `extensions/chrome-extension/`. The Samsung callback URL,
access token, and refresh token are credentials: never paste them into issues,
logs, screenshots, or support requests.

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

## Verification checklist

- Run the automatic flow once with the extension reloaded after installation.
- If the browser has an existing Samsung session, use the extension's **Reset
  Samsung Auth** action or sign out first so the flow starts with `status=new`.
- Confirm the SmartThings entry is created and devices appear.
- Allow one 30-second polling interval and confirm device state updates.
- If automatic sign-in fails, use the manual copy/paste path before collecting
  diagnostics; redact the full callback URL first.

# Original codeowner
Thanks to joostlek for handling the integration thus far
