# Configuration
This guide contains all the information you need to configure Auto-Southwest Check-In to your needs. A default/example configuration
file can be found at [config.example.json](config.example.json)

Auto-Southwest Check-In supports both global configuration and account/reservation-specific configuration. See
[Accounts and Reservations](#accounts-and-reservations) for more information.

**Note**: Many configuration items may also be configured via environment variables (except for account and
reservation-specific configurations).

## Table of Contents
- [Check Fares](#check-fares)
    * [Same-Day Smart Fare Checking](#same-day-smart-fare-checking)
- [Notifications](#notifications)
    * [Test the Notifications](#test-the-notifications)
- [Browser Path](#browser-path)
- [Retrieval Interval](#retrieval-interval)
- [Fare Watches](#fare-watches)
    * [Fare Watch Interval](#fare-watch-interval)
- [Ignore Server](#ignore-server)
    * [Ignore Server Port](#ignore-server-port)
    * [Ignore Server Base URL](#ignore-server-base-url)
    * [Ignore Server Token](#ignore-server-token)
- [Accounts and Reservations](#accounts-and-reservations)
    * [Accounts](#accounts)
    * [Reservations](#reservations)
        - [Companion Fare Points](#companion-fare-points)
        - [Original Fare Points](#original-fare-points)
        - [Original Taxes & Fees](#original-taxes--fees)
        - [Cached Flight Info](#cached-flight-info)
- [Healthchecks URL](#healthchecks-url)

## Check Fares
Default: true \
Type: Boolean or String \
Environment Variable: `AUTO_SOUTHWEST_CHECK_IN_CHECK_FARES`
> Using the environment variable will override the applicable setting in `config.json`.

In addition to automatically checking in, flights can be automatically checked for price drops on an interval
(see [Retrieval Interval](#retrieval-interval)). If a lower fare is found, the user will be notified.

**Note**: Companion passes are not supported for fare checking.
```json
{
    "check_fares": "<value>"
}
```

### Check Fares Values
- `false` or `"no"`: Do not check for lower fares
- `true` or `"same_flight"`: Check for lower fares on the same flight
- `"same_day_nonstop"`: Check for lower fares on all nonstop flights on the same day as the flight
- `"same_day"`: Check for lower fares on all flights on the same day as the flight
- `"same_day_smart"`: Find **all** cheaper same-day alternatives and send a single digest notification with ignore links (see [Same-Day Smart Fare Checking](#same-day-smart-fare-checking))

### Same-Day Smart Fare Checking

`same_day_smart` is an enhanced fare-checking mode that searches for every cheaper flight option on the same day as your current flight and emails you a digest with one-click ignore links.

**How it works:**
- Automatically detects whether your current flight is nonstop or has a connection. If your flight is nonstop, only nonstop alternatives are shown; if you have a connection, all alternatives (nonstop or connecting) are considered.
- Sends a single digest notification listing every cheaper option, sorted by biggest savings first.
- Each alternative includes a link to suppress that specific flight from future alerts, and a link to suppress all cheaper alternatives for that day.
- Ignore state is saved to `ignored_flights.json` in the project directory and survives restarts. Entries are automatically cleaned up once the flight date passes.
- Companion-pass flights are checked via public fare search. Set [`companionFarePoints`](#companion-fare-points) on the reservation to enable savings comparison.

**Requires the [Ignore Server](#ignore-server)** — a lightweight HTTP server that handles ignore-link clicks.

```json
{
    "check_fares": "same_day_smart"
}
```

Example notification:
```
Cheaper flights found for ABCDEF (LAX → STL on 2025-12-01) for John Doe!

Current flight: 100 at 8:40 AM

Cheaper options:
  200  6:00 AM  Nonstop  -2,300 PTS
    Ignore this flight: https://your-server/ignore?conf=ABCDEF&date=2025-12-01&flight=200&token=...

  300  11:15 AM  Nonstop  -1,500 PTS
    Ignore this flight: https://your-server/ignore?conf=ABCDEF&date=2025-12-01&flight=300&token=...

Ignore ALL alternates for 2025-12-01: https://your-server/ignore-all?conf=ABCDEF&date=2025-12-01&token=...
```

## Notifications
Default: [] \
Type: List \
Environment Variables:
- `AUTO_SOUTHWEST_CHECK_IN_NOTIFICATION_URL`
- `AUTO_SOUTHWEST_CHECK_IN_NOTIFICATION_LEVEL`
- `AUTO_SOUTHWEST_CHECK_IN_NOTIFICATION_24_HOUR_TIME`
> When using the environment variable, you may only specify a single URL. If a level or 24-hour time
> is specified, but no URL is specified, it will have no effect.
> If you are also using `config.json`, it will add the notification service as long as the URL is not a duplicate.

Users can be notified on successful and failed check-ins, flight scheduling, and fare drops. This is done through
the [Apprise library]. Information on how to create notification URLs can be found on the [Apprise Readme]. You can
optionally include a [notification level](#notification-level) and [24-hour time](#notification-24-hour-time) setting
for each notification service you use.
```json
{
  "notifications": [
    {"url": "service://my_first_service_url", "level": 3, "24_hour_time": true},
    {"url": "service://my_second_service_url"}
  ]
}
```

### Notification Level
Default: 2 \
Type: Integer

The following levels are available: \
`Level 1`: Receive notices of skipped reservation retrievals due to driver timeouts and Too Many Requests errors
during logins as well as all messages in later levels.\
`Level 2`: Receive successful scheduling messages, lower fare messages, and all messages in later levels.\
`Level 3`: Receive successful check-in messages, and all messages in later levels.\
`Level 4`: Receive only error messages (failed scheduling and check-ins).

### Notification 24 Hour Time
Default: false \
Type: Boolean

Display flight times in notifications in 24-hour format instead of 12-hour format. Console messages
will always display in 12-hour format.

### Test the Notifications
To test if the notification URLs work, you can run the following command
```shell
$ python3 southwest.py --test-notifications
```

## Browser Path
Default: The path to your Chrome or Chromium browser (if installed) \
Type: String \
Environment Variable: `AUTO_SOUTHWEST_CHECK_IN_BROWSER_PATH`
> Using the environment variable will override the applicable setting in `config.json`.

If you use another Chromium-based browser besides Google Chrome or Chromium (such as Brave), you need to specify the path to
the browser executable.

**Note**: Microsoft Edge is not supported
```json
{
    "browser_path": "/usr/bin/browser_path"
}
```

## Retrieval Interval
Default: 24 hours \
Type: Integer \
Environment Variable: `AUTO_SOUTHWEST_CHECK_IN_RETRIEVAL_INTERVAL`
> Using the environment variable will override the applicable setting in `config.json`.

You can choose how often the script checks for lower fares on scheduled flights (in hours). Additionally, this
interval will also determine how often the script checks for new flights if login credentials are provided. To
disable account/fare monitoring, set this option to `0` (The account/fares will only be checked once).
```json
{
    "retrieval_interval": 24
}
```

## Fare Watches
Type: List of objects

Fare watches track a route and date you have **not** booked, and email you when a flight's points price drops to or below a threshold. Unlike [Check Fares](#check-fares), no reservation or check-in is involved — this is purely a price alert for a route you might book later. Watches run automatically on [Fare Watch Interval](#fare-watch-interval), and can also be triggered on demand from the "Fare watches" page in the [Web UI](#web-ui).

Alerts are sent to your existing [notifications](#notifications) — there is no separate notification config per watch. Once a flight drops to or below the threshold, you're alerted once; you're alerted again only if it drops further, not on every check while it stays qualified.

```json
{
    "fare_watches": [
        {
            "name": "Thanksgiving MCO",
            "origin": "LGA",
            "destination": "MCO",
            "date": "2026-11-14",
            "maxPoints": 8000,
            "nonstopOnly": true,
            "fareTypes": ["WGA"],
            "flightNumbers": ["1234"]
        }
    ]
}
```

- `name` (optional, string): A label shown in the Web UI and notifications. Defaults to the watch's id.
- `origin` / `destination` (required, string): 3-letter airport codes.
- `date` (required, string): The one-way departure date, `YYYY-MM-DD`. A watch for a past date is automatically disabled. For a round trip, add a second watch for the return leg.
- `maxPoints` (required, positive integer): Alert when a flight's points price is at or below this number.
- `nonstopOnly` (optional, boolean, default `false`): Only consider nonstop flights.
- `fareTypes` (optional, list of strings): Restrict which fare products (e.g. `"WGA"`) are considered. Omit to use the cheapest fare product sold on each flight.
- `flightNumbers` (optional, list of strings): Restrict to specific flight numbers. Omit to consider every flight on that date.
- `enabled` (optional, boolean, default `true`): Set to `false` to keep a watch in the config without checking it.

### Fare Watch Interval
Default: same as [Retrieval Interval](#retrieval-interval) \
Type: Integer

How often (in hours) fare watches are automatically checked. Only takes effect when `fare_watches` is non-empty.
```json
{
    "fare_watch_interval": 12
}
```

## Ignore Server

The ignore server is a lightweight HTTP server embedded in the script that handles the one-click ignore links included in [`same_day_smart`](#same-day-smart-fare-checking) fare notifications. It starts automatically as a daemon thread when any account or reservation uses `same_day_smart`.

When running in Docker or behind a reverse proxy, you will need to expose the server's port and configure the base URL so the links in emails point to the correct public address.

### Ignore Server Port
Default: `8765` \
Type: Integer (1–65535)

The local port the ignore server listens on.
```json
{
    "ignoreServerPort": 8765
}
```

When running in Docker, expose this port in your `docker run` command or Compose file:
```yaml
ports:
  - "8765:8765"
```

### Ignore Server Base URL
Default: `http://localhost:{ignoreServerPort}` \
Type: String

The public base URL used to build ignore links in notifications. Set this when the server is accessible at a different address than `localhost` — for example, when running in Docker behind a Cloudflare tunnel or any other reverse proxy.

```json
{
    "ignoreServerBaseUrl": "https://sw-checker.example.com"
}
```

Any trailing slash is stripped automatically. The path and query string are appended by the script.

### Ignore Server Token
Default: None (no authentication) \
Type: String

A secret token that must be present in every ignore-link request. When set, the server returns `401 Unauthorized` for any request that omits or provides an incorrect `?token=` parameter. The token is automatically appended to every ignore link the script generates, so clicking a link from your email works without any extra steps.

```json
{
    "ignoreServerToken": "some-long-random-secret"
}
```

Anyone who receives the email can click the links (the token is embedded in the URL). Anyone who does not have a link cannot trigger an ignore without knowing the token. Choose a long random string — a UUID or a password manager-generated value works well.

#### Full same_day_smart + Docker + Cloudflare example
```json
{
    "check_fares": "same_day_smart",
    "ignoreServerPort": 8765,
    "ignoreServerBaseUrl": "https://sw-checker.example.com",
    "ignoreServerToken": "replace-with-a-long-random-secret",
    "reservations": [
        {"confirmationNumber": "ABC123", "firstName": "John", "lastName": "Doe"}
    ]
}
```

```yaml
# docker-compose.yml
services:
  auto-southwest:
    image: jdholtz/auto-southwest-check-in
    container_name: auto-southwest
    restart: on-failure
    ports:
      - "8765:8765"
    volumes:
      - /full-path/to/config.json:/app/config.json
      - /full-path/to/ignored_flights.json:/app/ignored_flights.json
```

Point your Cloudflare tunnel (or other reverse proxy) at `localhost:8765`.

## Accounts and Reservations
You can also add more [accounts](#accounts) and [reservations](#reservations) to the script through the configuration file.
Additionally, you can optionally specify [configuration options](#account-and-reservation-specific-configuration) for each
account and reservation.

### Accounts
Default: [] \
Type: List \
Environment Variables:
 - `AUTO_SOUTHWEST_CHECK_IN_USERNAME`
 - `AUTO_SOUTHWEST_CHECK_IN_PASSWORD`
> When using the environment variables, you may only specify a single set of credentials.

You can add more accounts to the script, allowing you to run multiple accounts at the same time and/or not
provide a username and password as arguments.
```json
{
    "accounts": [
        {"username": "user1", "password": "pass1"},
        {"username": "user2", "password": "pass2"}
    ]
}
```

### Reservations
Default: [] \
Type: List \
Environment Variables:
 - `AUTO_SOUTHWEST_CHECK_IN_CONFIRMATION_NUMBER`
 - `AUTO_SOUTHWEST_CHECK_IN_FIRST_NAME`
 - `AUTO_SOUTHWEST_CHECK_IN_LAST_NAME`
> When using the environment variables, you may only specify a single reservation.

You can also add more reservations to the script, allowing you check in to multiple reservations in the same instance
and/or not provide reservation information as arguments.
```json
{
    "reservations": [
        {"confirmationNumber": "num1", "firstName": "John", "lastName": "Doe"},
        {"confirmationNumber": "num2", "firstName": "Jane", "lastName": "Doe"}
    ]
}
```

#### Companion Fare Points
Default: None \
Type: Integer (reservation-only)

When a reservation has a companion pass attached, Southwest's change-shopping API is unavailable for fare checking. The script falls back to a public fare search to find the current market price. Set `companionFarePoints` to the number of points you originally paid for the companion fare so the script can calculate whether a cheaper option exists.

If `companionFarePoints` is not set, the script will still log the current market price but cannot determine whether it is lower than what you paid.

This option is only applicable to reservation configurations (not account configurations).

```json
{
    "reservations": [
        {
            "confirmationNumber": "ABCDEF",
            "firstName": "John",
            "lastName": "Doe",
            "companionFarePoints": 14000
        }
    ]
}
```

When used with `same_day_smart`, the script will also search for cheaper alternative flights for the companion reservation and include them in the digest notification.

#### Original Fare Points
Default: None \
Type: Integer (reservation-only)

Southwest's fare-check APIs only ever report the *difference* between today's price and what you originally paid, not an absolute figure. Set `originalFarePoints` to the number of points you originally paid for this reservation so the [Web UI](README.md#web-ui) can show the current fare found and the savings as absolute numbers, not just a delta.

This is purely for display in the web UI — it does not change any fare-checking behavior. If not set, the web UI will show the original fare as "not tracked" (or fall back to `companionFarePoints`, if that is set on a companion-pass reservation).

This value can also be set from the [web UI](README.md#editing-reservations) without editing this file by hand.

This option is only applicable to reservation configurations (not account configurations).

```json
{
    "reservations": [
        {
            "confirmationNumber": "ABCDEF",
            "firstName": "John",
            "lastName": "Doe",
            "originalFarePoints": 20000
        }
    ]
}
```

#### Original Taxes & Fees
Default: None \
Type: Number (reservation-only)

The taxes/fees (in USD) you originally paid for this reservation, shown alongside `originalFarePoints` in the [Web UI](README.md#web-ui). Like `originalFarePoints`, this is display-only and does not affect fare checking.

This option is only applicable to reservation configurations (not account configurations).

```json
{
    "reservations": [
        {
            "confirmationNumber": "ABCDEF",
            "firstName": "John",
            "lastName": "Doe",
            "originalFarePoints": 20000,
            "originalTaxesFees": 11.20
        }
    ]
}
```

#### Cached Flight Info
Default: None \
Type: String (reservation-only)

`cachedFlightNumber`, `cachedDepartureAirportCode`, `cachedDestinationAirportCode`, and
`cachedLocalDepartureDate` are written automatically by the [web UI](README.md#web-ui) the first
time it successfully checks a reservation, and kept up to date on every check after that. They let
the route, flight number, and date show up on the page immediately, without waiting for another
check. This is identity, not fare data — no pricing is ever stored here.

You don't need to set these by hand, and the check-in daemon itself never reads them — they exist
purely for the web UI's display.

This option is only applicable to reservation configurations (not account configurations).

### Account and Reservation-specific configuration
Setting specific configuration values for an account or reservation allows you to fully customize how you want them to be
monitored by the script. Here is a list of configuration values that can be applied to an individual account or reservation:
- [Check Fares](#check-fares)
- [Notifications](#notifications)
- [Retrieval Interval](#retrieval-interval)
- [Healthchecks URL](#healthchecks-url)

The following options apply globally only (top-level, not per account/reservation):
- [Ignore Server Port](#ignore-server-port)
- [Ignore Server Base URL](#ignore-server-base-url)
- [Ignore Server Token](#ignore-server-token)

Not all options have to be specified for each account or reservation. If an option is not specified, the top-level value is used
(or the default value if no top-level value is specified either) with exception to the Healthchecks URL. Any accounts or reservations
specified through the command line will use all of the top-level values.

An important note about notification services: An account or reservation with specific notification services will send notifications to those
services as well as services specified globally. If a service is in both the global and account/reservation configuration, the account/reservation
configuration will take precedence.

#### Examples
Here are a few examples of how the configuration options can be specified:

In this example, `user1`'s account will not check for lower flight fares. However, `user2`'s account will as the top-level value for
`check_fares` is `true`.
```json
{
    "check_fares": true,
    "accounts": [
        {"username": "user1", "password": "pass1", "check_fares": false},
        {"username": "user2", "password": "pass2"}
    ]
}
```

In this example, the script will send notifications attached to this reservation to both `top-level.url` and `my-special.url`.
```json
{
    "notifications": [{"url": "https://top-level.url"}],
    "reservations": [
        {
            "confirmationNumber": "num1",
            "firstName": "John",
            "lastName": "Doe",
            "notifications": [{"url": "https://my-special.url"}]
        }
    ]
}
```

## Healthchecks URL
Default: No URL \
Type: String

Monitor successful and failed fare checks using a [Healthchecks.io] URL. When a fare check
fails, the `/fail` endpoint of your Healthchecks URL will be pinged to notify you of the failure.

This configuration option can only be applied within reservation and account configurations (specifying it at the top-level
will have no effect). Due to this, no environment variable is provided as a replacement for this configuration option.
```json
{
    "accounts": [
        {
            "username": "user1",
            "password": "pass1",
            "healthchecks_url": "https://hc-ping.com/uuid"
        }
    ]
}
```


[Apprise library]: https://github.com/caronc/apprise
[Apprise Readme]: https://github.com/caronc/apprise#supported-notifications
[Healthchecks.io]: https://healthchecks.io
