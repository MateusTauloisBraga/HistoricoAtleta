# Strava Segment Head-to-Head

Compare athletes on a Strava segment: see the segment on a map, pick who to “battle”, and play an animated race with speed control.

## Features

- **Map**: Segment displayed on an interactive map.
- **Leaderboard**: Load and list athletes who completed the segment (via Strava API).
- **Head-to-head**: Select multiple athletes and run a “battle”.
- **Playback**: Play/pause and scrub through the race; positions are interpolated from each athlete’s total time.
- **Speed**: Control playback speed (e.g. 1x, 2x, 4x).

## Setup

1. **Strava API app**
   - Go to [Strava API Settings](https://www.strava.com/settings/api).
   - Create an application and note **Client ID** and **Client Secret**.

2. **Refresh token (must include \`read\` scope for leaderboards)**
   - Use the [Strava OAuth flow](https://developers.strava.com/docs/authentication/) to get an access token.
   - **Important:** The authorization URL must request the **`read`** scope (covers public segments, routes, and **leaderboards**). Example:
     ```
     https://www.strava.com/oauth/authorize?client_id=YOUR_CLIENT_ID&response_type=code&redirect_uri=http://localhost&approval_prompt=force&scope=read
     ```
     Set your app’s “Authorization Callback Domain” in [Strava API Settings](https://www.strava.com/settings/api) to `localhost` (or your redirect host). After you approve, Strava redirects to `http://localhost/?code=...`. Exchange that `code` (with your client ID and client secret) at `POST https://www.strava.com/oauth/token` to get `refresh_token` and `access_token`.
   - Or use a one-off script (e.g. [strava-token](https://github.com/sladkovm/strava-token)) and ensure it requests the **read** scope.
   - The app uses the refresh token to obtain access tokens automatically.

3. **Environment**
   - Copy `.env.example` to `.env`.
   - Fill in `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, and `STRAVA_REFRESH_TOKEN`.

4. **Run**
   ```bash
   pip install -r requirements.txt
   streamlit run app.py
   ```

## Segment URL

Default segment:  
`https://www.strava.com/activities/10390942897/segments/3171195555625707202`  
Segment ID: **3171195555625707202**. You can change the segment ID in the app.

## Leaderboard 403 Forbidden

If the segment loads but the leaderboard returns **403 Forbidden**, usually:

1. **Missing \`read\` scope** – Your refresh token was created without the `read` scope. Re-authorize using the URL above with `scope=read`, then get a new refresh token and put it in `.env`.
2. **Strava subscription** – Access to segment leaderboards via the API may require the **authenticated athlete** (the Strava account that authorized your app) to have an active **Strava subscription** (paid). Free accounts can get 403 on the leaderboard endpoint.

## Data source

Leaderboard data comes from the **Strava API** (no scraping). You need a Strava app and a refresh token. Athlete names on public leaderboards are abbreviated (e.g. “Paul M.”). Effort streams (exact GPS per effort) are only available for the authenticated athlete’s own efforts; for others we animate by moving each athlete along the segment path proportionally to elapsed time.

## Possible future features

- Filter leaderboard (e.g. friends, club, date range).
- Show your own effort as a “ghost” (when using your token).
- Share link to a specific battle (selected athletes + segment).
- Export comparison (e.g. screenshot or short summary).
