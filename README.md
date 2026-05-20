# iptv-epg-all

**One unified self-updating EPG for both IPTV subscriptions.** Merges sub1 + sub2 + bein-epg (highest-fidelity beIN MENA from beinsports.com) + epgshare01 community guides. Every channel in either M3U gets EPG data — real where available, "No EPG" dummy otherwise, with gap-fill for partially-covered channels.

## Paste this into your player

```
https://al7omed.github.io/iptv-epg-all/guide.xml.gz
```

Works alongside both M3Us. No filter — every group from both subscriptions is included.

## How it works

The build runs every 12 hours via GitHub Actions:

1. **Fetch sub1 M3U** (direct from provider's `get.php`).
2. **Fetch sub2 M3U** — tries direct first, falls back to Xtream API synthesis if the CDN's WAF blocks (HTTP 884 from some IPs).
3. **Combine M3Us** into one channel list. Channels with the same name across both subs collapse to the same `effective_id` (via deterministic name hash).
4. **Fetch upstream EPGs** in priority order — bein-epg (first-party beIN data) wins ties, then sub1 provider EPG, then sub2 provider EPG, then 7 epgshare01 region files.
5. **Backfill pass**: rewire upstream channel IDs to M3U `effective_id`s so the player can actually bind to them.
6. **Dummy pass**: every M3U entry not yet covered gets a "No EPG" placeholder, broken into 4-hour blocks for 8 days (some players refuse to render programmes > 24h).
7. **Gap-fill pass**: real channels with coverage holes get dummy blocks filling the gaps. No more "data unavailable" mid-day.
8. Writes `docs/guide.xml.gz` (~25 MB) and `docs/tvg-id-map.tsv`.

All snapped to GMT+3 hour boundaries.

## Patching your M3Us

Your subscriptions have many channels without `tvg-id` in the original M3U. To bind them to the EPG, patch each M3U locally:

```sh
curl -fsSL https://raw.githubusercontent.com/al7omed/iptv-epg-all/main/scripts/patch_m3u.py -o ~/patch_m3u.py

# fetch sub1 locally, patch, use patched file in player
curl -fsSL "<sub1 M3U URL>" -o ~/sub1.m3u
python3 ~/patch_m3u.py ~/sub1.m3u ~/sub1_patched.m3u

# same for sub2
curl -fsSL "<sub2 M3U URL>" -o ~/sub2.m3u  # use Xtream synth if blocked
python3 ~/patch_m3u.py ~/sub2.m3u ~/sub2_patched.m3u
```

The map file the script downloads (`tvg-id-map.tsv`) is non-sensitive — channel-name → tvg-id only, no URLs.

## Configuration (GitHub Secrets)

- `M3U_URL_1`, `PROVIDER_EPG_URL_1` — first subscription.
- `M3U_URL_2`, `PROVIDER_EPG_URL_2` — second subscription.
- `XTREAM_HOST_2`, `XTREAM_USER_2`, `XTREAM_PASS_2` — for sub2 M3U synth fallback.

## Sister repos

- [iptv-epg](https://github.com/al7omed/iptv-epg) — sub1 only, kept as a reference build.
- [iptv-epg-2](https://github.com/al7omed/iptv-epg-2) — sub2 only.
- [bein-epg](https://github.com/al7omed/bein-epg) — focused beIN MENA scraper, consumed by this repo.
