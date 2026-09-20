# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# Twitter ads  account report

## Specification

PROJECT OVERVIEW — Twitter ads account report

This project builds three daily Twitter Ads reporting marts: twitter_ads__account_report, twitter_ads__keyword_report and twitter_ads__promoted_tweet_report.

Source tables and their extraction backends. The table account_history must be extracted from the mongodb backend; each of its records represents a version of each account, the versions can be differentiated by the updated_at timestamp, id uniquely identifies a row, at most one row per id is present, rows are used exactly as supplied and no history-version selection or version deduplication is performed. The table campaign_history must be extracted from the s3 backend; each of its records represents a version of each campaign, differentiated by the updated_at timestamp, and rows are used exactly as supplied with no history-version selection or version deduplication. The table line_item_history must be extracted from the postgres backend; each of its records represents a version of each line item, id uniquely identifies a row, at most one row per id is present, and rows are used exactly as supplied with no history-version selection or version deduplication. The table line_item_keywords_report must be extracted from the rest backend; each of its records represents the performance of a line item (ad group) and keyword combination on a given day. The table promoted_tweet_history must be extracted from the mongodb backend; each of its records represents a version of each promoted tweet, id uniquely identifies a row, at most one row per id is present, and rows are used exactly as supplied with no history-version selection or version deduplication. The table promoted_tweet_report must be extracted from the mongodb backend; each of its records represents the performance of a promoted tweet on a given day, in its defined placement. The table tweet must be extracted from the mongodb backend; each of its records represents a tweet, promoted or not.

Relationships between the source tables, exactly as the source schema declares them. The child table line_item_history with key campaign_id refers to the parent table campaign_history with key id, and this relationship is optional (may be NULL or dangling). The child table line_item_keywords_report with key account_id refers to the parent table account_history with key id, and this relationship is optional (may be NULL or dangling). The child table line_item_keywords_report with key line_item_id refers to the parent table line_item_history with key id, and this relationship is optional (may be NULL or dangling). The child table promoted_tweet_history with key line_item_id refers to the parent table line_item_history with key id, and this relationship is optional (may be NULL or dangling). The child table promoted_tweet_history with key tweet_id refers to the parent table tweet with key id, and this relationship is optional (may be NULL or dangling). The child table promoted_tweet_report with key account_id refers to the parent table account_history with key id, and this relationship is optional (may be NULL or dangling). The child table promoted_tweet_report with key promoted_tweet_id refers to the parent table promoted_tweet_history with key id, and this relationship is optional (may be NULL or dangling).

General conventions used in all three marts. A day value called date_day is the source date truncated to the start of its calendar day at 00:00:00. Wherever a measure is described as accumulated over the source rows that share the mart row's grain, the measure is the total of that source value across exactly those rows.

=== Mart twitter_ads__account_report — each record in this table represents the daily performance of ads at the account level, within a placement in Twitter. ===

Grain: one row per account_id, placement, date_day. Each record in this table represents the daily performance of ads at the account level, within a placement in Twitter. The carried columns account_name, approval_status, business_id, business_name, created_timestamp, industry_type, is_deleted, timezone, timezone_switched_timestamp, updated_timestamp are part of this grain together with these keys, so rows that agree on the keys but differ in one of them are separate output rows.

Key columns: account_id, placement and date_day.

Rule 1: the rows of source table promoted_tweet_report are read and supply the performance measures of this mart.

Rule 2: the rows of source table account_history are read and supply the account attributes of this mart.

Rule 3: the mart key columns account_id, placement, date_day come from source table promoted_tweet_report; date_day is not copied from a promoted_tweet_report column but computed from promoted_tweet_report, being its date truncated to the start of its calendar day at 00:00:00, and it remains part of the mart grain.

Rule 4: each promoted_tweet_report row is matched to its account_history row across the relationship the package declares, matching the account_id of promoted_tweet_report to the id of account_history; account_history is the parent side, preservation is left-sided, so every promoted_tweet_report row appears exactly once, including promoted_tweet_report rows with no account_history row, and account_id and id are the carried columns of this matching.

Rule 5 (accumulation): there is one output row per account_id, placement, date_day together with the carried account_name, approval_status, business_id, business_name, created_timestamp, industry_type, is_deleted, timezone, timezone_switched_timestamp, updated_timestamp, which are part of the grain — source rows that agree on the key columns but differ in a carried column fall in different output rows — and each such row reports clicks, conversion_custom_metric, conversion_custom_sale_amount, conversion_purchases_metric, conversion_purchases_sale_amount, impressions, spend, spend_micro, url_clicks for that row's matching rows. These accumulated outputs have no declared replacement for an empty result: clicks, impressions, spend, spend_micro, url_clicks; an empty result is preserved as empty, not 0, a missing input value contributes nothing, and an output whose matching input values are all missing is empty, whether it reads every matching row or only the rows that qualify for its condition, while every other output follows its own declared column rule, and these accumulated outputs count a missing input value as 0, so a row whose matching values are all missing reports 0, never empty: conversion_custom_metric, conversion_custom_sale_amount, conversion_purchases_metric, conversion_purchases_sale_amount.

Rule 6 (naming): the mart columns carry exactly these published names: account_id, placement, date_day, account_name, approval_status, business_id, business_name, created_timestamp, industry_type, is_deleted, timezone, timezone_switched_timestamp, updated_timestamp, clicks, conversion_custom_metric, conversion_custom_sale_amount, conversion_purchases_metric, conversion_purchases_sale_amount, impressions, spend, spend_micro, url_clicks.

Rule 7 (deterministic output order): rows appear sorted ascending by account_id, then placement, then date_day.

Output columns of twitter_ads__account_report:
- account_id (text): the ID of the account.
- placement (text): where on Twitter the ad is being displayed; possible values include 'ALL_ON_TWITTER', 'PUBLISHER_NETWORK', 'TWITTER_PROFILE', 'TWITTER_SEARCH', 'TWITTER_TIMELINE', and 'TAP_*', which are more granular options for `PUBLISHER_NETWORK`.
- date_day (timestamp): the date of the performance; it is date truncated to the start of its calendar day at 00:00:00 and remains part of the mart grain.
- account_name (text): name of the account, taken as the `name` of the matching `account_history` record.
- approval_status (text): the approval status of the account, taken as the `approval_status` of the matching `account_history` record.
- business_id (text): the ID of the related business, taken as the `business_id` of the matching `account_history` record.
- business_name (text): the name of the related business, taken as the `business_name` of the matching `account_history` record.
- created_timestamp (timestamp): the timestamp the account was created, taken as the `created_at` of the matching `account_history` record.
- industry_type (text): the industry of the accounts, taken as the `industry_type` of the matching `account_history` record.
- is_deleted (boolean): whether the record has been deleted or not, taken as the `deleted` of the matching `account_history` record.
- timezone (text): the timezone the account is set to, taken as the `timezone` of the matching `account_history` record.
- timezone_switched_timestamp (timestamp): the timestamp the account's timezone was last changed, taken as the `timezone_switch_at` of the matching `account_history` record.
- updated_timestamp (timestamp): the timestamp the account was last updated, taken as the `updated_at` of the matching `account_history` record.
- clicks (bigint): the clicks for th account on that day, including clicks on the URL (shortened or regular links), profile pic, screen name, username, detail, hashtags, and likes; per-input source lineage, `clicks` is the value of source column `promoted_tweet_report.clicks`, added up over the source rows that share the mart row's grain.
- conversion_custom_metric (bigint): the number of conversions of type CUSTOM, included by the `twitter_ads__conversion_fields` variable by default; per-input source lineage, `conversion_custom_metric` is the value of source column `promoted_tweet_report.conversion_custom_metric`, added up over the source rows that share the mart row's grain.
- conversion_custom_sale_amount (float): the sale amount corresponding to PURCHASE conversion events, included by the `twitter_ads__conversion_sale_amount_fields` variable by default; per-input source lineage, `conversion_custom_sale_amount` is the value of source column `promoted_tweet_report.conversion_custom_sale_amount`, added up over the source rows that share the mart row's grain.
- conversion_purchases_metric (bigint): total number of purchases, the sum of post view, post engagement, and assisted purchases for both your website and mobile app, included by the `twitter_ads__conversion_fields` variable by default; per-input source lineage, `conversion_purchases_metric` is the value of source column `promoted_tweet_report.conversion_purchases_metric`, added up over the source rows that share the mart row's grain.
- conversion_purchases_sale_amount (float): the sale amount corresponding to PURCHASE conversion events, included by the `twitter_ads__conversion_sale_amount_fields` variable by default; per-input source lineage, `conversion_purchases_sale_amount` is the value of source column `promoted_tweet_report.conversion_purchases_sale_amount`, added up over the source rows that share the mart row's grain.
- impressions (bigint): the impressions for the account on that day, which is the number of users who see a Promoted Ad either in their home timeline or search results; per-input source lineage, `impressions` is the value of source column `promoted_tweet_report.impressions`, added up over the source rows that share the mart row's grain.
- spend (float): the spend for the account on that day; units converted out of promoted_tweet_report.billed_charge_local_micro — each source value is divided by 1,000,000 and rounded to 2 decimal places, and those values are then added together, a source row with no value adding nothing to the total, and a mart row whose source rows all lack a value reports an empty total, not 0; per-input source lineage, `billed_charge_local_micro` is the value of source column `promoted_tweet_report.billed_charge_local_micro`.
- spend_micro (bigint): the spend (in micros) for the account on that day; per-input source lineage, `spend_micro` is the value of source column `promoted_tweet_report.billed_charge_local_micro`, added up over the source rows that share the mart row's grain.
- url_clicks (bigint): the url clicks for the account on that day; per-input source lineage, `url_clicks` is the value of source column `promoted_tweet_report.url_clicks`, added up over the source rows that share the mart row's grain.

=== Mart twitter_ads__keyword_report — each record in this table represents the daily performance of ads at the account, campaign, line item (ad group), and keyword level, within a placement in Twitter. ===

Grain: one row per account_id, keyword, line_item_id, placement, date_day, keyword_id. Each record in this table represents the daily performance of ads at the account, campaign, line item (ad group), and keyword level, within a placement in Twitter. The carried columns account_name, campaign_id, currency, line_item_name are part of this grain together with these keys, so rows that agree on the keys but differ in one of them are separate output rows.

Key columns: account_id, keyword, line_item_id, placement, date_day and keyword_id.

Rule 1: the rows of source table line_item_keywords_report are read and supply the performance measures of this mart.

Rule 2: the rows of source table line_item_history are read and supply the line item attributes of this mart.

Rule 3: the rows of source table account_history are read and supply the account attributes of this mart.

Rule 4: source table campaign_history is read for extraction only — no rule of this mart matches or reads its rows, and it adds no rows and no columns to the mart.

Rule 5: the mart key columns account_id, keyword, line_item_id, placement, date_day, keyword_id come from source table line_item_keywords_report; date_day is not copied from a line_item_keywords_report column but computed from line_item_keywords_report, being its date truncated to the start of its calendar day at 00:00:00, and it remains part of the mart grain, keyword_id is likewise not copied from a line_item_keywords_report column but computed from line_item_keywords_report, computed before accumulation and remaining part of the mart grain, and keyword is the value of the segment column of line_item_keywords_report.

Rule 6: each line_item_keywords_report row is matched to its line_item_history row across the relationship the package declares, matching the line_item_id of line_item_keywords_report to the id of line_item_history; line_item_history is the parent side, preservation is left-sided, so every line_item_keywords_report row appears exactly once, including line_item_keywords_report rows with no line_item_history row, and line_item_id and id are the carried columns of this matching.

Rule 7: each line_item_keywords_report row is matched to its account_history row across the relationship the package declares, matching the account_id of line_item_keywords_report to the id of account_history; account_history is the parent side, preservation is left-sided, so every line_item_keywords_report row appears exactly once, including line_item_keywords_report rows with no account_history row, and account_id and id are the carried columns of this matching.

Rule 8 (accumulation): there is one output row per account_id, keyword, line_item_id, placement, date_day, keyword_id together with the carried account_name, campaign_id, currency, line_item_name, which are part of the grain — source rows that agree on the key columns but differ in a carried column fall in different output rows — and each such row reports clicks, conversion_custom_metric, conversion_custom_sale_amount, conversion_purchases_metric, conversion_purchases_sale_amount, impressions, spend, spend_micro, total_conversions, total_conversions_sale_amount, url_clicks for that row's matching rows. These accumulated outputs have no declared replacement for an empty result: clicks, impressions, spend, spend_micro, url_clicks; an empty result is preserved as empty, not 0, a missing input value contributes nothing, and an output whose matching input values are all missing is empty, whether it reads every matching row or only the rows that qualify for its condition, while every other output follows its own declared column rule, and these accumulated outputs count a missing input value as 0, so a row whose matching values are all missing reports 0, never empty: conversion_custom_metric, conversion_custom_sale_amount, conversion_purchases_metric, conversion_purchases_sale_amount, total_conversions, total_conversions_sale_amount.

Rule 9 (naming): the mart columns carry exactly these published names: account_id, keyword, line_item_id, placement, date_day, keyword_id, account_name, campaign_id, currency, line_item_name, clicks, conversion_custom_metric, conversion_custom_sale_amount, conversion_purchases_metric, conversion_purchases_sale_amount, impressions, spend, spend_micro, total_conversions, total_conversions_sale_amount, url_clicks.

Rule 10 (deterministic output order): rows appear sorted ascending by account_id, then keyword, then line_item_id, then placement, then date_day, then keyword_id.

Output columns of twitter_ads__keyword_report:
- account_id (text): the ID of the related account.
- keyword (text): the keyword whose performance is being tracked.
- line_item_id (text): the ID of the related line item (ad group).
- placement (text): where on Twitter the ad is being displayed; possible values include 'ALL_ON_TWITTER', 'PUBLISHER_NETWORK', 'TWITTER_PROFILE', 'TWITTER_SEARCH', 'TWITTER_TIMELINE', and 'TAP_*', which are more granular options for `PUBLISHER_NETWORK`.
- date_day (timestamp): the date of the performance; it is date truncated to the start of its calendar day at 00:00:00 and remains part of the mart grain.
- keyword_id (text): to form keyword_id, take account_id, line_item_id, segment, placement in that order, render each value as text, replace a missing value with '_dbt_utils_surrogate_key_null_', and place '-' between adjacent values; the result is the lowercase 32-character hexadecimal MD5 digest of that combined text; it is computed before accumulation and remains part of the mart grain.
- account_name (text): name of the associated account, taken as the `name` of the matching `account_history` record.
- campaign_id (text): the ID of the related campaign, taken as the `campaign_id` of the matching `line_item_history` record.
- currency (text): the currency all metrics for the account are set to, taken as the `currency` of the matching `line_item_history` record.
- line_item_name (text): the ID of the related line item, taken as the `name` of the matching `line_item_history` record.
- clicks (bigint): the clicks for the line item + keyword on that day, including clicks on the URL (shortened or regular links), profile pic, screen name, username, detail, hashtags, and likes; per-input source lineage, `clicks` is the value of source column `line_item_keywords_report.clicks`, added up over the source rows that share the mart row's grain.
- conversion_custom_metric (bigint): the number of conversions of type CUSTOM, included by the `twitter_ads__conversion_fields` variable by default; per-input source lineage, `conversion_custom_metric` is the value of source column `line_item_keywords_report.conversion_custom_metric`, added up over the source rows that share the mart row's grain.
- conversion_custom_sale_amount (float): the sale amount corresponding to PURCHASE conversion events, included by the `twitter_ads__conversion_sale_amount_fields` variable by default; per-input source lineage, `conversion_custom_sale_amount` is the value of source column `line_item_keywords_report.conversion_custom_sale_amount`, added up over the source rows that share the mart row's grain.
- conversion_purchases_metric (bigint): total number of purchases, the sum of post view, post engagement, and assisted purchases for both your website and mobile app, included by the `twitter_ads__conversion_fields` variable by default; per-input source lineage, `conversion_purchases_metric` is the value of source column `line_item_keywords_report.conversion_purchases_metric`, added up over the source rows that share the mart row's grain.
- conversion_purchases_sale_amount (float): the sale amount corresponding to PURCHASE conversion events, included by the `twitter_ads__conversion_sale_amount_fields` variable by default; per-input source lineage, `conversion_purchases_sale_amount` is the value of source column `line_item_keywords_report.conversion_purchases_sale_amount`, added up over the source rows that share the mart row's grain.
- impressions (bigint): the impressions for the line item + keyword on that day, which is the number of users who see a Promoted Ad either in their home timeline or search results; per-input source lineage, `impressions` is the value of source column `line_item_keywords_report.impressions`, added up over the source rows that share the mart row's grain.
- spend (float): the spend for the line item + keyword on that day in whichever currency was selected during account creation; units converted out of the micros of line_item_keywords_report.billed_charge_local_micro — each source value is divided by 1,000,000 and rounded to 2 decimal places, and those values are then added together, a source row with no value adding nothing to the total, and a mart row whose source rows all lack a value reports an empty total, not 0; per-input source lineage, `billed_charge_local_micro` is the value of source column `line_item_keywords_report.billed_charge_local_micro`.
- spend_micro (bigint): the spend for the line item + keyword on that day, in micros and in whichever currency was selected during account creation; per-input source lineage, `spend_micro` is the value of source column `line_item_keywords_report.billed_charge_local_micro`, added up over the source rows that share the mart row's grain.
- total_conversions (bigint): sum of all fields included in `twitter_ads__conversion_fields` variable (this mart uses exactly conversion_purchases_metric + conversion_custom_metric); this exact per-input component list is complete and authoritative for this column, every named component remains an input even if it is not emitted as a separate mart output; for each input row, missing conversion_purchases_metric, conversion_custom_metric values contribute 0 before the components are added, and the resulting row values are then accumulated for the mart grain; per-input source lineage, `conversion_custom_metric` is the value of source column `line_item_keywords_report.conversion_custom_metric`, `conversion_purchases_metric` is the value of source column `line_item_keywords_report.conversion_purchases_metric`, added up over the source rows that share the mart row's grain.
- total_conversions_sale_amount (float): sum of all fields included in `twitter_ads__conversion_sale_amount_fields` variable (this mart uses exactly conversion_purchases_sale_amount + conversion_custom_sale_amount + conversion_sign_ups_sale_amount); this exact per-input component list is complete and authoritative for this column, every named component remains an input even if it is not emitted as a separate mart output; for each input row, missing conversion_purchases_sale_amount, conversion_custom_sale_amount, conversion_sign_ups_sale_amount values contribute 0 before the components are added, and the resulting row values are then accumulated for the mart grain; per-input source lineage, `conversion_custom_sale_amount` is the value of source column `line_item_keywords_report.conversion_custom_sale_amount`, `conversion_purchases_sale_amount` is the value of source column `line_item_keywords_report.conversion_purchases_sale_amount`, `conversion_sign_ups_sale_amount` is the value of source column `line_item_keywords_report.conversion_sign_ups_sale_amount`, added up over the source rows that share the mart row's grain.
- url_clicks (bigint): the url clicks for the line item + keyword on that day; per-input source lineage, `url_clicks` is the value of source column `line_item_keywords_report.url_clicks`, added up over the source rows that share the mart row's grain.

=== Mart twitter_ads__promoted_tweet_report — each record in this table represents the daily performance of ads at the account, campaign, line item (ad group), and promoted tweet level, within a placement in Twitter. ===

Grain: one row per placement, promoted_tweet_id, account_id, date_day. Each record in this table represents the daily performance of ads at the account, campaign, line item (ad group), and promoted tweet level, within a placement in Twitter. The carried columns account_name, approval_status, created_timestamp, is_deleted, line_item_id, promoted_tweet_status, tweet_id, updated_timestamp are part of this grain together with these keys, so rows that agree on the keys but differ in one of them are separate output rows.

Key columns: placement, promoted_tweet_id, account_id and date_day.

Rule 1: the rows of source table promoted_tweet_report are read and supply the performance measures of this mart.

Rule 2: the rows of source table promoted_tweet_history are read and supply the promoted tweet attributes of this mart.

Rule 3: the rows of source table account_history are read and supply the account attributes of this mart.

Rule 4: source table campaign_history is read for extraction only — no rule of this mart matches or reads its rows, and it adds no rows and no columns to the mart.

Rule 5: source table line_item_history is read for extraction only — no rule of this mart matches or reads its rows, and it adds no rows and no columns to the mart.

Rule 6: source table tweet is read for extraction only — no rule of this mart matches or reads its rows, and it adds no rows and no columns to the mart.

Rule 7: the mart key columns placement, promoted_tweet_id, account_id, date_day come from source table promoted_tweet_report; date_day is not copied from a promoted_tweet_report column but computed from promoted_tweet_report, being its date truncated to the start of its calendar day at 00:00:00, and it remains part of the mart grain.

Rule 8: each promoted_tweet_report row is matched to its promoted_tweet_history row across the relationship the package declares, matching the promoted_tweet_id of promoted_tweet_report to the id of promoted_tweet_history; promoted_tweet_history is the parent side, preservation is left-sided, so every promoted_tweet_report row appears exactly once, including promoted_tweet_report rows with no promoted_tweet_history row, and promoted_tweet_id and id are the carried columns of this matching.

Rule 9: each promoted_tweet_report row is matched to its account_history row across the relationship the package declares, matching the account_id of promoted_tweet_report to the id of account_history; account_history is the parent side, preservation is left-sided, so every promoted_tweet_report row appears exactly once, including promoted_tweet_report rows with no account_history row, and account_id and id are the carried columns of this matching.

Rule 10 (accumulation): there is one output row per placement, promoted_tweet_id, account_id, date_day together with the carried account_name, approval_status, created_timestamp, is_deleted, line_item_id, promoted_tweet_status, tweet_id, updated_timestamp, which are part of the grain — source rows that agree on the key columns but differ in a carried column fall in different output rows — and each such row reports clicks, conversion_custom_metric, conversion_custom_sale_amount, conversion_purchases_metric, conversion_purchases_sale_amount, impressions, spend, spend_micro, total_conversions, total_conversions_sale_amount, url_clicks for that row's matching rows. These accumulated outputs have no declared replacement for an empty result: clicks, impressions, spend, spend_micro, url_clicks; an empty result is preserved as empty, not 0, a missing input value contributes nothing, and an output whose matching input values are all missing is empty, whether it reads every matching row or only the rows that qualify for its condition, while every other output follows its own declared column rule, and these accumulated outputs count a missing input value as 0, so a row whose matching values are all missing reports 0, never empty: conversion_custom_metric, conversion_custom_sale_amount, conversion_purchases_metric, conversion_purchases_sale_amount, total_conversions, total_conversions_sale_amount.

Rule 11 (naming): the mart columns carry exactly these published names: placement, promoted_tweet_id, account_id, date_day, account_name, approval_status, created_timestamp, is_deleted, line_item_id, promoted_tweet_status, tweet_id, updated_timestamp, clicks, conversion_custom_metric, conversion_custom_sale_amount, conversion_purchases_metric, conversion_purchases_sale_amount, impressions, spend, spend_micro, total_conversions, total_conversions_sale_amount, url_clicks.

Rule 12 (deterministic output order): rows appear sorted ascending by placement, then promoted_tweet_id, then account_id, then date_day.

Output columns of twitter_ads__promoted_tweet_report:
- placement (text): where on Twitter the ad is being displayed; possible values include 'ALL_ON_TWITTER', 'PUBLISHER_NETWORK', 'TWITTER_PROFILE', 'TWITTER_SEARCH', 'TWITTER_TIMELINE', and 'TAP_*', which are more granular options for `PUBLISHER_NETWORK`.
- promoted_tweet_id (text): the ID of the promoted tweet that the URL appeared in.
- account_id (text): the ID of the related account.
- date_day (timestamp): the date of the performance; it is date truncated to the start of its calendar day at 00:00:00 and remains part of the mart grain.
- account_name (text): the name of the related account, taken as the `name` of the matching `account_history` record.
- approval_status (text): the approval status of the promoted tweet, taken as the `approval_status` of the matching `promoted_tweet_history` record.
- created_timestamp (timestamp): the timestamp the account was created, taken as the `created_at` of the matching `promoted_tweet_history` record.
- is_deleted (boolean): whether the record has been deleted or not, taken as the `deleted` of the matching `promoted_tweet_history` record.
- line_item_id (text): the ID of the related line item (ad group), taken as the `line_item_id` of the matching `promoted_tweet_history` record.
- promoted_tweet_status (text): the status of the promoted tweet, taken as the `entity_status` of the matching `promoted_tweet_history` record.
- tweet_id (text): the ID of the tweet that the URL appeared in, taken as the `tweet_id` of the matching `promoted_tweet_history` record.
- updated_timestamp (timestamp): the timestamp the account was last updated, taken as the `updated_at` of the matching `promoted_tweet_history` record.
- clicks (bigint): the clicks for the promoted tweet + URL on that day, including clicks on the URL (shortened or regular links), profile pic, screen name, username, detail, hashtags, and likes; per-input source lineage, `clicks` is the value of source column `promoted_tweet_report.clicks`, added up over the source rows that share the mart row's grain.
- conversion_custom_metric (bigint): the number of conversions of type CUSTOM, included by the `twitter_ads__conversion_fields` variable by default; per-input source lineage, `conversion_custom_metric` is the value of source column `promoted_tweet_report.conversion_custom_metric`, added up over the source rows that share the mart row's grain.
- conversion_custom_sale_amount (float): the sale amount corresponding to PURCHASE conversion events, included by the `twitter_ads__conversion_sale_amount_fields` variable by default; per-input source lineage, `conversion_custom_sale_amount` is the value of source column `promoted_tweet_report.conversion_custom_sale_amount`, added up over the source rows that share the mart row's grain.
- conversion_purchases_metric (bigint): total number of purchases, the sum of post view, post engagement, and assisted purchases for both your website and mobile app, included by the `twitter_ads__conversion_fields` variable by default; per-input source lineage, `conversion_purchases_metric` is the value of source column `promoted_tweet_report.conversion_purchases_metric`, added up over the source rows that share the mart row's grain.
- conversion_purchases_sale_amount (float): the sale amount corresponding to PURCHASE conversion events, included by the `twitter_ads__conversion_sale_amount_fields` variable by default; per-input source lineage, `conversion_purchases_sale_amount` is the value of source column `promoted_tweet_report.conversion_purchases_sale_amount`, added up over the source rows that share the mart row's grain.
- impressions (bigint): the impressions for the promoted tweet + URL on that day, which is the number of users who see a Promoted Ad either in their home timeline or search results; per-input source lineage, `impressions` is the value of source column `promoted_tweet_report.impressions`, added up over the source rows that share the mart row's grain.
- spend (float): the spend for the promoted tweet + URL on that day; units converted out of promoted_tweet_report.billed_charge_local_micro — each source value is divided by 1,000,000 and rounded to 2 decimal places, and those values are then added together, a source row with no value adding nothing to the total, and a mart row whose source rows all lack a value reports an empty total, not 0; per-input source lineage, `billed_charge_local_micro` is the value of source column `promoted_tweet_report.billed_charge_local_micro`.
- spend_micro (bigint): the spend, in micros, for the tweet + URL on that day; per-input source lineage, `spend_micro` is the value of source column `promoted_tweet_report.billed_charge_local_micro`, added up over the source rows that share the mart row's grain.
- total_conversions (bigint): sum of all fields included in `twitter_ads__conversion_fields` variable (this mart uses exactly conversion_purchases_metric + conversion_custom_metric); this exact per-input component list is complete and authoritative for this column, every named component remains an input even if it is not emitted as a separate mart output; for each input row, missing conversion_purchases_metric, conversion_custom_metric values contribute 0 before the components are added, and the resulting row values are then accumulated for the mart grain; per-input source lineage, `conversion_custom_metric` is the value of source column `promoted_tweet_report.conversion_custom_metric`, `conversion_purchases_metric` is the value of source column `promoted_tweet_report.conversion_purchases_metric`, added up over the source rows that share the mart row's grain.
- total_conversions_sale_amount (float): sum of all fields included in `twitter_ads__conversion_sale_amount_fields` variable (this mart uses exactly conversion_purchases_sale_amount + conversion_custom_sale_amount + conversion_sign_ups_sale_amount); this exact per-input component list is complete and authoritative for this column, every named component remains an input even if it is not emitted as a separate mart output; for each input row, missing conversion_purchases_sale_amount, conversion_custom_sale_amount, conversion_sign_ups_sale_amount values contribute 0 before the components are added, and the resulting row values are then accumulated for the mart grain; per-input source lineage, `conversion_custom_sale_amount` is the value of source column `promoted_tweet_report.conversion_custom_sale_amount`, `conversion_purchases_sale_amount` is the value of source column `promoted_tweet_report.conversion_purchases_sale_amount`, `conversion_sign_ups_sale_amount` is the value of source column `promoted_tweet_report.conversion_sign_ups_sale_amount`, added up over the source rows that share the mart row's grain.
- url_clicks (bigint): the URL clicks for the promoted tweet + URL on that day; per-input source lineage, `url_clicks` is the value of source column `promoted_tweet_report.url_clicks`, added up over the source rows that share the mart row's grain.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `twitter_ads__account_report`

- Grain: One row per account_id, placement, date_day. Each record in this table represents the daily performance of ads at the account level, within a placement in Twitter. The carried columns account_name, approval_status, business_id, business_name, created_timestamp, industry_type, is_deleted, timezone, timezone_switched_timestamp, updated_timestamp are part of this grain together with these keys, so rows that agree on the keys but differ in one of them are separate output rows.
- Unique key: account_id, placement, date_day
- Required columns: account_id, placement, date_day, account_name, approval_status, business_id, business_name, created_timestamp, industry_type, is_deleted, timezone, timezone_switched_timestamp, updated_timestamp, clicks, conversion_custom_metric, conversion_custom_sale_amount, conversion_purchases_metric, conversion_purchases_sale_amount, impressions, spend, spend_micro, url_clicks

```text
Mart 'twitter_ads__account_report' has 7 declared semantic rules:
1. [source] Read source table promoted_tweet_report. (public source tables: promoted_tweet_report)
2. [source] Read source table account_history. (public source tables: account_history)
3. [derive] Form the mart key columns account_id, placement, date_day from source table promoted_tweet_report. date_day is not copied from a promoted_tweet_report column but computed from promoted_tweet_report: It is date truncated to the start of its calendar day at 00:00:00 and remains part of the mart grain. (public source tables: promoted_tweet_report | public carried/output columns: account_id, placement, date_day)
4. [join] Each promoted_tweet_report row is matched to its account_history row across the relationship the package declares; account_history is the parent side, so every promoted_tweet_report row appears exactly once, including promoted_tweet_report rows with no account_history row. (public source tables: account_history | public carried/output columns: account_id, id | join preservation: left | condition public identifiers: account_history, id, account_id)
5. [aggregate] One output row per account_id, placement, date_day together with the carried account_name, approval_status, business_id, business_name, created_timestamp, industry_type, is_deleted, timezone, timezone_switched_timestamp, updated_timestamp, which are part of the grain: source rows that agree on the key columns but differ in a carried column fall in different output rows, reporting clicks, conversion_custom_metric, conversion_custom_sale_amount, conversion_purchases_metric, conversion_purchases_sale_amount, impressions, spend, spend_micro, url_clicks for that row's matching rows. These aggregate outputs have no declared replacement for an empty result: clicks, impressions, spend, spend_micro, url_clicks. Preserve an empty result as empty, not 0: a missing input value contributes nothing, and an output whose matching input values are all missing is empty, whether it reads every matching row or only the rows that qualify for its condition. Every other output follows its own declared column rule. These aggregate outputs count a missing input value as 0, so a row whose matching values are all missing reports 0, never empty: conversion_custom_metric, conversion_custom_sale_amount, conversion_purchases_metric, conversion_purchases_sale_amount. (public carried/output columns: account_id, placement, date_day, account_name, approval_status, business_id, business_name, created_timestamp, industry_type, is_deleted, timezone, timezone_switched_timestamp, updated_timestamp, clicks, conversion_custom_metric, conversion_custom_sale_amount, conversion_purchases_metric, conversion_purchases_sale_amount, impressions, spend, spend_micro, url_clicks)
6. [derive] Name the mart columns. (public carried/output columns: account_id, placement, date_day, account_name, approval_status, business_id, business_name, created_timestamp, industry_type, is_deleted, timezone, timezone_switched_timestamp, updated_timestamp, clicks, conversion_custom_metric, conversion_custom_sale_amount, conversion_purchases_metric, conversion_purchases_sale_amount, impressions, spend, spend_micro, url_clicks)
7. [tie_break] Deterministic output order: sort by account_id, placement, date_day. (public carried/output columns: account_id, placement, date_day)
```

### `twitter_ads__keyword_report`

- Grain: One row per account_id, keyword, line_item_id, placement, date_day, keyword_id. Each record in this table represents the daily performance of ads at the account, campaign, line item (ad group), and keyword level, within a placement in Twitter. The carried columns account_name, campaign_id, currency, line_item_name are part of this grain together with these keys, so rows that agree on the keys but differ in one of them are separate output rows.
- Unique key: account_id, keyword, line_item_id, placement, date_day, keyword_id
- Required columns: account_id, keyword, line_item_id, placement, date_day, keyword_id, account_name, campaign_id, currency, line_item_name, clicks, conversion_custom_metric, conversion_custom_sale_amount, conversion_purchases_metric, conversion_purchases_sale_amount, impressions, spend, spend_micro, total_conversions, total_conversions_sale_amount, url_clicks

```text
Mart 'twitter_ads__keyword_report' has 10 declared semantic rules:
1. [source] Read source table line_item_keywords_report. (public source tables: line_item_keywords_report)
2. [source] Read source table line_item_history. (public source tables: line_item_history)
3. [source] Read source table account_history. (public source tables: account_history)
4. [source] Read source table campaign_history for extraction only: no rule of this mart matches or reads its rows, and it adds no rows and no columns to the mart. (public source tables: campaign_history)
5. [derive] Form the mart key columns account_id, keyword, line_item_id, placement, date_day, keyword_id from source table line_item_keywords_report. date_day is not copied from a line_item_keywords_report column but computed from line_item_keywords_report: It is date truncated to the start of its calendar day at 00:00:00 and remains part of the mart grain. keyword_id is not copied from a line_item_keywords_report column but computed from line_item_keywords_report: It is computed before accumulation and remains part of the mart grain. keyword is the value of the segment column of line_item_keywords_report. (public source tables: line_item_keywords_report | public carried/output columns: account_id, keyword, line_item_id, placement, date_day, keyword_id)
6. [join] Each line_item_keywords_report row is matched to its line_item_history row across the relationship the package declares; line_item_history is the parent side, so every line_item_keywords_report row appears exactly once, including line_item_keywords_report rows with no line_item_history row. (public source tables: line_item_history | public carried/output columns: line_item_id, id | join preservation: left | condition public identifiers: line_item_history, id, line_item_id)
7. [join] Each line_item_keywords_report row is matched to its account_history row across the relationship the package declares; account_history is the parent side, so every line_item_keywords_report row appears exactly once, including line_item_keywords_report rows with no account_history row. (public source tables: account_history | public carried/output columns: account_id, id | join preservation: left | condition public identifiers: account_history, id, account_id)
8. [aggregate] One output row per account_id, keyword, line_item_id, placement, date_day, keyword_id together with the carried account_name, campaign_id, currency, line_item_name, which are part of the grain: source rows that agree on the key columns but differ in a carried column fall in different output rows, reporting clicks, conversion_custom_metric, conversion_custom_sale_amount, conversion_purchases_metric, conversion_purchases_sale_amount, impressions, spend, spend_micro, total_conversions, total_conversions_sale_amount, url_clicks for that row's matching rows. These aggregate outputs have no declared replacement for an empty result: clicks, impressions, spend, spend_micro, url_clicks. Preserve an empty result as empty, not 0: a missing input value contributes nothing, and an output whose matching input values are all missing is empty, whether it reads every matching row or only the rows that qualify for its condition. Every other output follows its own declared column rule. These aggregate outputs count a missing input value as 0, so a row whose matching values are all missing reports 0, never empty: conversion_custom_metric, conversion_custom_sale_amount, conversion_purchases_metric, conversion_purchases_sale_amount, total_conversions, total_conversions_sale_amount. (public carried/output columns: account_id, keyword, line_item_id, placement, date_day, keyword_id, account_name, campaign_id, currency, line_item_name, clicks, conversion_custom_metric, conversion_custom_sale_amount, conversion_purchases_metric, conversion_purchases_sale_amount, impressions, spend, spend_micro, total_conversions, total_conversions_sale_amount, url_clicks)
9. [derive] Name the mart columns. (public carried/output columns: account_id, keyword, line_item_id, placement, date_day, keyword_id, account_name, campaign_id, currency, line_item_name, clicks, conversion_custom_metric, conversion_custom_sale_amount, conversion_purchases_metric, conversion_purchases_sale_amount, impressions, spend, spend_micro, total_conversions, total_conversions_sale_amount, url_clicks)
10. [tie_break] Deterministic output order: sort by account_id, keyword, line_item_id, placement, date_day, keyword_id. (public carried/output columns: account_id, keyword, line_item_id, placement, date_day, keyword_id)
```

### `twitter_ads__promoted_tweet_report`

- Grain: One row per placement, promoted_tweet_id, account_id, date_day. Each record in this table represents the daily performance of ads at the account, campaign, line item (ad group), and promoted tweet level, within a placement in Twitter. The carried columns account_name, approval_status, created_timestamp, is_deleted, line_item_id, promoted_tweet_status, tweet_id, updated_timestamp are part of this grain together with these keys, so rows that agree on the keys but differ in one of them are separate output rows.
- Unique key: placement, promoted_tweet_id, account_id, date_day
- Required columns: placement, promoted_tweet_id, account_id, date_day, account_name, approval_status, created_timestamp, is_deleted, line_item_id, promoted_tweet_status, tweet_id, updated_timestamp, clicks, conversion_custom_metric, conversion_custom_sale_amount, conversion_purchases_metric, conversion_purchases_sale_amount, impressions, spend, spend_micro, total_conversions, total_conversions_sale_amount, url_clicks

```text
Mart 'twitter_ads__promoted_tweet_report' has 12 declared semantic rules:
1. [source] Read source table promoted_tweet_report. (public source tables: promoted_tweet_report)
2. [source] Read source table promoted_tweet_history. (public source tables: promoted_tweet_history)
3. [source] Read source table account_history. (public source tables: account_history)
4. [source] Read source table campaign_history for extraction only: no rule of this mart matches or reads its rows, and it adds no rows and no columns to the mart. (public source tables: campaign_history)
5. [source] Read source table line_item_history for extraction only: no rule of this mart matches or reads its rows, and it adds no rows and no columns to the mart. (public source tables: line_item_history)
6. [source] Read source table tweet for extraction only: no rule of this mart matches or reads its rows, and it adds no rows and no columns to the mart. (public source tables: tweet)
7. [derive] Form the mart key columns placement, promoted_tweet_id, account_id, date_day from source table promoted_tweet_report. date_day is not copied from a promoted_tweet_report column but computed from promoted_tweet_report: It is date truncated to the start of its calendar day at 00:00:00 and remains part of the mart grain. (public source tables: promoted_tweet_report | public carried/output columns: placement, promoted_tweet_id, account_id, date_day)
8. [join] Each promoted_tweet_report row is matched to its promoted_tweet_history row across the relationship the package declares; promoted_tweet_history is the parent side, so every promoted_tweet_report row appears exactly once, including promoted_tweet_report rows with no promoted_tweet_history row. (public source tables: promoted_tweet_history | public carried/output columns: promoted_tweet_id, id | join preservation: left | condition public identifiers: promoted_tweet_history, id, promoted_tweet_id)
9. [join] Each promoted_tweet_report row is matched to its account_history row across the relationship the package declares; account_history is the parent side, so every promoted_tweet_report row appears exactly once, including promoted_tweet_report rows with no account_history row. (public source tables: account_history | public carried/output columns: account_id, id | join preservation: left | condition public identifiers: account_history, id, account_id)
10. [aggregate] One output row per placement, promoted_tweet_id, account_id, date_day together with the carried account_name, approval_status, created_timestamp, is_deleted, line_item_id, promoted_tweet_status, tweet_id, updated_timestamp, which are part of the grain: source rows that agree on the key columns but differ in a carried column fall in different output rows, reporting clicks, conversion_custom_metric, conversion_custom_sale_amount, conversion_purchases_metric, conversion_purchases_sale_amount, impressions, spend, spend_micro, total_conversions, total_conversions_sale_amount, url_clicks for that row's matching rows. These aggregate outputs have no declared replacement for an empty result: clicks, impressions, spend, spend_micro, url_clicks. Preserve an empty result as empty, not 0: a missing input value contributes nothing, and an output whose matching input values are all missing is empty, whether it reads every matching row or only the rows that qualify for its condition. Every other output follows its own declared column rule. These aggregate outputs count a missing input value as 0, so a row whose matching values are all missing reports 0, never empty: conversion_custom_metric, conversion_custom_sale_amount, conversion_purchases_metric, conversion_purchases_sale_amount, total_conversions, total_conversions_sale_amount. (public carried/output columns: placement, promoted_tweet_id, account_id, date_day, account_name, approval_status, created_timestamp, is_deleted, line_item_id, promoted_tweet_status, tweet_id, updated_timestamp, clicks, conversion_custom_metric, conversion_custom_sale_amount, conversion_purchases_metric, conversion_purchases_sale_amount, impressions, spend, spend_micro, total_conversions, total_conversions_sale_amount, url_clicks)
11. [derive] Name the mart columns. (public carried/output columns: placement, promoted_tweet_id, account_id, date_day, account_name, approval_status, created_timestamp, is_deleted, line_item_id, promoted_tweet_status, tweet_id, updated_timestamp, clicks, conversion_custom_metric, conversion_custom_sale_amount, conversion_purchases_metric, conversion_purchases_sale_amount, impressions, spend, spend_micro, total_conversions, total_conversions_sale_amount, url_clicks)
12. [tie_break] Deterministic output order: sort by placement, promoted_tweet_id, account_id, date_day. (public carried/output columns: placement, promoted_tweet_id, account_id, date_day)
```

## Source tables

### account_history  (source backend: mongodb)
Each record represents a version of each account. The versions can be differentiated by the updated_at timestamp. In this generated task, id uniquely identifies a row and at most one row per id is present. Rows are used exactly as supplied in this generated task; no history-version selection or version deduplication is performed.

- `_fivetran_synced`: text NULL — When the record was last synced by Fivetran.
- `approval_status`: text NULL — The approval status of the account.
- `business_id`: text NULL — The ID of the related business.
- `business_name`: text NULL — The name of the related business.
- `created_at`: timestamp NULL — The timestamp the account was created.
- `deleted`: boolean NULL — Whether the record has been deleted or not.
- `id`: text NOT NULL — The ID of the account.
- `industry_type`: text NULL — The industry of the accounts.
- `name`: text NULL — The name of the account.
- `salt`: text NULL — The random encryption key used to has data.
- `timezone`: text NULL — The timezone the account is set to.
- `timezone_switch_at`: timestamp NULL — The timestamp the account's timezone was last changed.
- `updated_at`: timestamp NULL — The timestamp the account was last updated.
- primary key: id

### campaign_history  (source backend: s3)
Each record represents a version of each campaign. The versions can be differentiated by the updated_at timestamp. Rows are used exactly as supplied in this generated task; no history-version selection or version deduplication is performed.

- `_fivetran_synced`: text NULL — When the record was last synced by Fivetran.
- `account_id`: text NULL — The ID of the related account.
- `created_at`: timestamp NULL — The timestamp the account was created.
- `currency`: text NULL — The currently all metrics for the account are set to.
- `daily_budget_amount_local_micro`: integer NULL — The daily budget amount to be allocated to the campaign. The currency associated with the specified funding instrument will be used.
- `deleted`: boolean NULL — Whether the record has been deleted or not.
- `duration_in_days`: integer NULL — The time period within which the frequency_cap is achieved.
- `end_time`: timestamp NULL — The time the campaign will end
- `entity_status`: text NULL — The status of the campaign.
- `frequency_cap`: integer NULL — The maximum number of times an ad could be delivered to a user.
- `funding_instrument_id`: text NULL — Reference to the funding instrument.
- `id`: text NULL — The ID of the campaign.
- `name`: text NULL — The name of the campaign.
- `servable`: boolean NULL — Whether the campaign is in a state to be actively served to users.
- `standard_delivery`: boolean NULL — Whether standard delivery is enabled (vs accelerated delivery).
- `start_time`: timestamp NULL — The time the campaign will start.
- `total_budget_amount_local_micro`: integer NULL — The total budget amount to be allocated to the campaign.
- `updated_at`: timestamp NULL — The timestamp the account was last updated.

### line_item_history  (source backend: postgres)
Each record represents a version of each line item. The versions can be differentiated by the updated_at timestamp. In this generated task, id uniquely identifies a row and at most one row per id is present. Rows are used exactly as supplied in this generated task; no history-version selection or version deduplication is performed.

- `_fivetran_synced`: text NULL — When the record was last synced by Fivetran.
- `advertiser_domain`: text NULL — The website domain for this advertiser, without the protocol specification.
- `advertiser_user_id`: integer NULL — The Twitter user identifier for the handle promoting the ad.
- `automatically_select_bid`: boolean NULL — Whether automatically optimize bidding is enabled based on daily budget and campaign flight dates.
- `bid_amount_local_micro`: integer NULL — The bid amount to be associated with this line item.
- `bid_type`: text NULL — The bidding mechanism.
- `bid_unit`: text NULL — The bid unit for this line item.
- `campaign_id`: text NULL — The ID of the related campaign.
- `charge_by`: text NULL — The unit to charge this line item by.
- `created_at`: timestamp NULL — The timestamp the account was created.
- `creative_source`: text NULL — The source of the creatives for the line item.
- `currency`: text NULL — The currency in which metrics will be reported.
- `deleted`: boolean NULL — Whether the record has been deleted or not.
- `end_time`: timestamp NULL — The timestamp at which the line item will stop being served.
- `entity_status`: text NULL — The status of the line item.
- `id`: text NOT NULL — The ID of the line item.
- `name`: text NULL — The name of the line item.
- `objective`: text NULL — The campaign objective for this line item.
- `optimization`: text NULL — The optimization setting to use with this line item.
- `primary_web_event_tag`: text NULL — The identifier of the primary web event tag. Allows more accurate tracking of engagements for the campaign pertaining to this line item.
- `product_type`: text NULL — The type of promoted product that this line item will contain.
- `start_time`: timestamp NULL — The timestamp at which the line item will start being served.
- `target_cpa_local_micro`: integer NULL — The target cost per acquisition for the line item.
- `total_budget_amount_local_micro`: integer NULL — The total budget amount to be allocated to the line item.
- `updated_at`: timestamp NULL — The timestamp the account was last updated.
- primary key: id

### line_item_keywords_report  (source backend: rest)
Each record represents the performance of a line item (ad group) and keyword combination on a given day.

- `_fivetran_synced`: text NULL — When the record was last synced by Fivetran.
- `account_id`: text NOT NULL — The ID of the related account.
- `billed_charge_local_micro`: integer NULL — The spend for the line item + keyword on that day, in micros and in whichever currency was selected during account creation.
- `clicks`: integer NULL — The clicks for the line item + keyword on that day. Includes clicks on the URL (shortened or regular links), profile pic, screen name, username, detail, hashtags, and likes.
- `conversion_custom_metric`: bigint NULL — The number of conversions of type CUSTOM. Included by the `twitter_ads__conversion_fields` variable by default.
- `conversion_custom_sale_amount`: float NULL — The sale amount corresponding to PURCHASE conversion events. Included by the `twitter_ads__conversion_sale_amount_fields` variable by default.
- `conversion_purchases_metric`: bigint NULL — Total number of purchases. The sum of post view, post engagement, and assisted purchases for both your website and mobile app. Included by the `twitter_ads__conversion_fields` variable by default.
- `conversion_purchases_sale_amount`: float NULL — The sale amount corresponding to PURCHASE conversion events. Included by the `twitter_ads__conversion_sale_amount_fields` variable by default.
- `conversion_sign_ups_metric`: text NULL — Total number of sign ups. This is the same as the sum of post views, post engagements and assisted sign ups. This is also the sum of website and mobile app sign ups
- `conversion_sign_ups_sale_amount`: float NULL — The sale amount corresponding to sign ups.
- `date`: timestamp NOT NULL — The date of the performance.
- `impressions`: integer NULL — The impressions for the line item + keyword on that day.  This is the number of users who see a Promoted Ad either in their home timeline or search results.
- `line_item_id`: text NOT NULL — The ID of the line item.
- `mobile_conversion_add_to_carts_assisted`: text NULL — Number of **assisted** (engaged with ad but did not immediately convert) mobile conversions of type ADD_TO_CART.
- `mobile_conversion_add_to_carts_post_engagement`: text NULL — Number of **post-engagement** mobile conversions of type ADD_TO_CART.
- `mobile_conversion_add_to_carts_post_view`: text NULL — Number of **post-view** mobile conversions of type ADD_TO_CART.
- `mobile_conversion_add_to_carts_sale_amount`: text NULL — The sale amount corresponding to mobile conversions of type ADD_TO_CART.
- `mobile_conversion_add_to_wishlists_assisted`: text NULL — Number of **assisted** (engaged with ad but did not immediately convert) mobile conversions of type ADD_TO_WISHLIST.
- `mobile_conversion_add_to_wishlists_post_engagement`: text NULL — Number of **post-engagement** mobile conversions of type ADD_TO_WISHLIST.
- `mobile_conversion_add_to_wishlists_post_view`: text NULL — Number of **post-view** mobile conversions of type ADD_TO_WISHLIST.
- `mobile_conversion_add_to_wishlists_sale_amount`: text NULL — The sale amount corresponding to mobile conversions of type ADD_TO_WISHLIST.
- `mobile_conversion_checkouts_initiated_assisted`: text NULL — Number of **assisted** (engaged with ad but did not immediately convert) mobile conversions of type CHECKOUT_INITIATED.
- `mobile_conversion_checkouts_initiated_post_engagement`: text NULL — Number of **post-engagement** mobile conversions of type CHECKOUT_INITIATED.
- `mobile_conversion_checkouts_initiated_post_view`: text NULL — Number of **post-view** mobile conversions of type CHECKOUT_INITIATED.
- `mobile_conversion_checkouts_initiated_sale_amount`: text NULL — The sale amount corresponding to mobile conversions of type CHECKOUT_INITIATED.
- `mobile_conversion_content_views_assisted`: text NULL — Number of **assisted** (engaged with ad but did not immediately convert) mobile conversions of type CONTENT_VIEW.
- `mobile_conversion_content_views_post_engagement`: text NULL — Number of **post-engagement** mobile conversions of type CONTENT_VIEW.
- `mobile_conversion_content_views_post_view`: text NULL — Number of **post-view** mobile conversions of type CONTENT_VIEW.
- `mobile_conversion_content_views_sale_amount`: text NULL — The sale amount corresponding to mobile conversions of type CONTENT_VIEW.
- `mobile_conversion_payment_info_additions_assisted`: text NULL — Number of **assisted** (engaged with ad but did not immediately convert) mobile conversions of type PAYMENT_INFO_ADDITION.
- `mobile_conversion_payment_info_additions_post_engagement`: text NULL — Number of **post-engagement** mobile conversions of type PAYMENT_INFO_ADDITION.
- `mobile_conversion_payment_info_additions_post_view`: text NULL — Number of **post-view** mobile conversions of type PAYMENT_INFO_ADDITION.
- `mobile_conversion_payment_info_additions_sale_amount`: text NULL — The sale amount corresponding to mobile conversions of type PAYMENT_INFO_ADDITION.
- `mobile_conversion_searches_assisted`: text NULL — Number of **assisted** (engaged with ad but did not immediately convert) mobile conversions of type SEARCH.
- `mobile_conversion_searches_post_engagement`: text NULL — Number of **post-engagement** mobile conversions of type SEARCH.
- `mobile_conversion_searches_post_view`: text NULL — Number of **post-view** mobile conversions of type SEARCH.
- `mobile_conversion_searches_sale_amount`: text NULL — The sale amount corresponding to mobile conversions of type SEARCH.
- `placement`: text NOT NULL — Where on Twitter the ad is being displayed. Possible values include 'ALL_ON_TWITTER', 'PUBLISHER_NETWORK', 'TWITTER_PROFILE', 'TWITTER_SEARCH', 'TWITTER_TIMELINE', and 'TAP_*', which are more granular options for `PUBLISHER_NETWORK`.
- `segment`: text NOT NULL — The keyword whose performance is being tracked.
- `url_clicks`: integer NULL — The url clicks for the line item + keyword on that day.

### promoted_tweet_history  (source backend: mongodb)
Each record represents a version of each promoted tweet. The versions can be differentiated by the updated_at timestamp. In this generated task, id uniquely identifies a row and at most one row per id is present. Rows are used exactly as supplied in this generated task; no history-version selection or version deduplication is performed.

- `_fivetran_synced`: text NULL — When the record was last synced by Fivetran.
- `approval_status`: text NULL — The approval status of the promoted tweet.
- `created_at`: timestamp NULL — The timestamp the account was created.
- `deleted`: boolean NULL — Whether the record has been deleted or not.
- `entity_status`: text NULL — The status of the promoted tweet.
- `id`: text NOT NULL — The ID of the promoted tweet.
- `line_item_id`: text NULL — The ID of the related line item.
- `tweet_id`: text NULL — The ID of the related tweet.
- `updated_at`: timestamp NULL — The timestamp the account was last updated.
- primary key: id

### promoted_tweet_report  (source backend: mongodb)
Each record represents the performance of a promoted tweet on a given day, in its defined placement.

- `_fivetran_synced`: text NULL — When the record was last synced by Fivetran.
- `account_id`: text NOT NULL — The ID of the related account.
- `billed_charge_local_micro`: integer NULL — The spend for the promoted tweet on that day.
- `clicks`: integer NULL — The clicks for the promoted tweet on that day. Includes clicks on the URL (shortened or regular links), profile pic, screen name, username, detail, hashtags, and likes.
- `conversion_custom_metric`: bigint NULL — The number of conversions of type CUSTOM. Included by the `twitter_ads__conversion_fields` variable by default.
- `conversion_custom_sale_amount`: float NULL — The sale amount corresponding to PURCHASE conversion events. Included by the `twitter_ads__conversion_sale_amount_fields` variable by default.
- `conversion_purchases_metric`: bigint NULL — Total number of purchases. The sum of post view, post engagement, and assisted purchases for both your website and mobile app. Included by the `twitter_ads__conversion_fields` variable by default.
- `conversion_purchases_sale_amount`: float NULL — The sale amount corresponding to PURCHASE conversion events. Included by the `twitter_ads__conversion_sale_amount_fields` variable by default.
- `conversion_sign_ups_metric`: text NULL — Total number of sign ups. This is the same as the sum of post views, post engagements and assisted sign ups. This is also the sum of website and mobile app sign ups
- `conversion_sign_ups_sale_amount`: float NULL — The sale amount corresponding to sign ups.
- `date`: timestamp NOT NULL — The date of the performance.
- `impressions`: integer NULL — The impressions for the promoted tweet on that day.  This is the number of users who see a Promoted Ad either in their home timeline or search results.
- `mobile_conversion_add_to_carts_assisted`: text NULL — Number of **assisted** (engaged with ad but did not immediately convert) mobile conversions of type ADD_TO_CART.
- `mobile_conversion_add_to_carts_post_engagement`: text NULL — Number of **post-engagement** mobile conversions of type ADD_TO_CART.
- `mobile_conversion_add_to_carts_post_view`: text NULL — Number of **post-view** mobile conversions of type ADD_TO_CART.
- `mobile_conversion_add_to_carts_sale_amount`: text NULL — The sale amount corresponding to mobile conversions of type ADD_TO_CART.
- `mobile_conversion_add_to_wishlists_assisted`: text NULL — Number of **assisted** (engaged with ad but did not immediately convert) mobile conversions of type ADD_TO_WISHLIST.
- `mobile_conversion_add_to_wishlists_post_engagement`: text NULL — Number of **post-engagement** mobile conversions of type ADD_TO_WISHLIST.
- `mobile_conversion_add_to_wishlists_post_view`: text NULL — Number of **post-view** mobile conversions of type ADD_TO_WISHLIST.
- `mobile_conversion_add_to_wishlists_sale_amount`: text NULL — The sale amount corresponding to mobile conversions of type ADD_TO_WISHLIST.
- `mobile_conversion_checkouts_initiated_assisted`: text NULL — Number of **assisted** (engaged with ad but did not immediately convert) mobile conversions of type CHECKOUT_INITIATED.
- `mobile_conversion_checkouts_initiated_post_engagement`: text NULL — Number of **post-engagement** mobile conversions of type CHECKOUT_INITIATED.
- `mobile_conversion_checkouts_initiated_post_view`: text NULL — Number of **post-view** mobile conversions of type CHECKOUT_INITIATED.
- `mobile_conversion_checkouts_initiated_sale_amount`: text NULL — The sale amount corresponding to mobile conversions of type CHECKOUT_INITIATED.
- `mobile_conversion_content_views_assisted`: text NULL — Number of **assisted** (engaged with ad but did not immediately convert) mobile conversions of type CONTENT_VIEW.
- `mobile_conversion_content_views_post_engagement`: text NULL — Number of **post-engagement** mobile conversions of type CONTENT_VIEW.
- `mobile_conversion_content_views_post_view`: text NULL — Number of **post-view** mobile conversions of type CONTENT_VIEW.
- `mobile_conversion_content_views_sale_amount`: text NULL — The sale amount corresponding to mobile conversions of type CONTENT_VIEW.
- `mobile_conversion_payment_info_additions_assisted`: text NULL — Number of **assisted** (engaged with ad but did not immediately convert) mobile conversions of type PAYMENT_INFO_ADDITION.
- `mobile_conversion_payment_info_additions_post_engagement`: text NULL — Number of **post-engagement** mobile conversions of type PAYMENT_INFO_ADDITION.
- `mobile_conversion_payment_info_additions_post_view`: text NULL — Number of **post-view** mobile conversions of type PAYMENT_INFO_ADDITION.
- `mobile_conversion_payment_info_additions_sale_amount`: text NULL — The sale amount corresponding to mobile conversions of type PAYMENT_INFO_ADDITION.
- `mobile_conversion_searches_assisted`: text NULL — Number of **assisted** (engaged with ad but did not immediately convert) mobile conversions of type SEARCH.
- `mobile_conversion_searches_post_engagement`: text NULL — Number of **post-engagement** mobile conversions of type SEARCH.
- `mobile_conversion_searches_post_view`: text NULL — Number of **post-view** mobile conversions of type SEARCH.
- `mobile_conversion_searches_sale_amount`: text NULL — The sale amount corresponding to mobile conversions of type SEARCH.
- `placement`: text NOT NULL — Where on Twitter the ad is being displayed. Possible values include 'ALL_ON_TWITTER', 'PUBLISHER_NETWORK', 'TWITTER_PROFILE', 'TWITTER_SEARCH', 'TWITTER_TIMELINE', and 'TAP_*', which are more granular options for `PUBLISHER_NETWORK`.
- `promoted_tweet_id`: text NOT NULL — The ID of the related promoted tweet.
- `url_clicks`: integer NULL — The url clicks for the promoted tweet on that day.

### tweet  (source backend: mongodb)
Each record represents a tweet, promoted or not.

- `_fivetran_synced`: text NULL — When the record was last synced by Fivetran.
- `account_id`: text NULL — The ID of the related account.
- `full_text`: text NULL — Full text of the tweet's content.
- `id`: text NULL — Unique identifier of the tweet.
- `lang`: text NULL — Two-letter language code of the tweet.
- `name`: text NULL — If provided, the non-public title of the tweet.

### Relationships

- line_item_history(campaign_id) -> campaign_history(id) [optional (may be NULL/dangling)]
- line_item_keywords_report(account_id) -> account_history(id) [optional (may be NULL/dangling)]
- line_item_keywords_report(line_item_id) -> line_item_history(id) [optional (may be NULL/dangling)]
- promoted_tweet_history(line_item_id) -> line_item_history(id) [optional (may be NULL/dangling)]
- promoted_tweet_history(tweet_id) -> tweet(id) [optional (may be NULL/dangling)]
- promoted_tweet_report(account_id) -> account_history(id) [optional (may be NULL/dangling)]
- promoted_tweet_report(promoted_tweet_id) -> promoted_tweet_history(id) [optional (may be NULL/dangling)]

