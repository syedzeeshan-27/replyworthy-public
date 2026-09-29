-- 0006 social links: cross-source social profiles per lead — ONE json object
-- ({"facebook"|"instagram"|"x"|"linkedin"|"tiktok": canonical url}, shaped by
-- tools/extract_socials.py). Captured from the Maps contact crawl plus the
-- already-fetched site content; feeds the pipeline channel filter, the Excel
-- columns, and the DM queue. NULL means "none known" (an empty scrape never
-- blanks a stored value — lead_db enforces it).

ALTER TABLE leads ADD COLUMN social_links TEXT;
