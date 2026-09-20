# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# SchemaPile schema: Github Com Lea Video Db

## Specification

PROJECT OVERVIEW

This project builds one analytical mart, picture_video_distribution, from the Github Com Lea Video Db schema (the 079208_leavideo.ddl schema). A solver must read each source table from the extraction backend named for it below, and only from that backend.

Source tables and their extraction backends:
- Source table actor must be extracted from the files backend. It holds celebrityid and videoid.
- Source table artist must be extracted from the s3 backend. It holds celebrityid and videoid.
- Source table celebrity must be extracted from the s3 backend. It holds celebrityid and the nullable avatarpictureid.
- Source table celebrity_picture must be extracted from the mongodb backend. It holds celebrityid and pictureid.
- Source table celebrity_tag must be extracted from the s3 backend. It holds celebrityid and tagid.
- Source table galerie must be extracted from the s3 backend. It holds galerieid.
- Source table galerie_name must be extracted from the s3 backend. It holds galerieid and nameid.
- Source table galerie_picture must be extracted from the mongodb backend. It holds galerieid and pictureid.
- Source table group must be extracted from the files backend. It holds groupid and parentgroupid.
- Source table media must be extracted from the postgres backend. It holds mediaid, videovideoid, audiobitrate, audioencoding, hasaudio, hasvideo, videobitrate and videoencoding.
- Source table movie must be extracted from the files backend. It holds imdbid and the nullable videoid.
- Source table name must be extracted from the files backend. It holds nameid and the nullable language and name.
- Source table name_celebrity must be extracted from the postgres backend. It holds celebrityid and nameid.
- Source table picture must be extracted from the postgres backend. It holds pictureid and the nullable format, height and width.
- Source table picture_name must be extracted from the mongodb backend. It holds nameid and pictureid.
- Source table playlist must be extracted from the postgres backend. It holds playlistid.
- Source table playlistitem must be extracted from the files backend. It holds playlistitemid, playlistid and the nullable galerieid and videoid.
- Source table playlist_name must be extracted from the s3 backend. It holds nameid and playlistid.
- Source table regisseur must be extracted from the rest backend. It holds celebrityid and videoid.
- Source table snippet must be extracted from the rest backend. It holds videoid, sourcevideoid, startframe and endframe.
- Source table tag must be extracted from the mongodb backend. It holds tagid and the nullable parenttagid and defaulttag.
- Source table tag_galerie must be extracted from the postgres backend. It holds galerieid and tagid.
- Source table tag_group must be extracted from the mongodb backend. It holds groupid and tagid.
- Source table tag_name must be extracted from the rest backend. It holds nameid and tagid.
- Source table tag_picture must be extracted from the postgres backend. It holds pictureid and tagid.
- Source table tag_playlist must be extracted from the rest backend. It holds playlistid and tagid.
- Source table tag_video must be extracted from the postgres backend. It holds tagid and videoid.
- Source table user must be extracted from the postgres backend. It holds userid and the nullable password and salt.
- Source table user_group must be extracted from the s3 backend. It holds groupid and userid.
- Source table variation must be extracted from the files backend. It holds variationid.
- Source table variation_name must be extracted from the s3 backend. It holds nameid and variationid.
- Source table video must be extracted from the postgres backend. It holds videoid, lengthseconds and the nullable thumbnailpictureid.
- Source table video_name must be extracted from the rest backend. It holds nameid and videoid.
- Source table video_variation must be extracted from the s3 backend. It holds variationid, videoid, isdefaultaudio and isdefaultvideo.

RELATIONSHIPS BETWEEN SOURCE TABLES

Each statement below names the child table with its key columns and the parent table with its key columns, and is labelled required or optional exactly as the source schema declares it.

- Child table actor on celebrityid refers to parent table celebrity on celebrityid: required.
- Child table actor on videoid refers to parent table video on videoid: required.
- Child table artist on celebrityid refers to parent table celebrity on celebrityid: required.
- Child table artist on videoid refers to parent table video on videoid: required.
- Child table celebrity on avatarpictureid refers to parent table picture on pictureid: optional (may be NULL or dangling).
- Child table celebrity_picture on celebrityid refers to parent table celebrity on celebrityid: required.
- Child table celebrity_picture on pictureid refers to parent table picture on pictureid: required.
- Child table celebrity_tag on celebrityid refers to parent table celebrity on celebrityid: required.
- Child table celebrity_tag on tagid refers to parent table tag on tagid: required.
- Child table galerie_name on galerieid refers to parent table galerie on galerieid: required.
- Child table galerie_name on nameid refers to parent table name on nameid: required.
- Child table galerie_picture on galerieid refers to parent table galerie on galerieid: required.
- Child table galerie_picture on pictureid refers to parent table picture on pictureid: required.
- Child table media on videovideoid refers to parent table video on videoid: required.
- Child table movie on videoid refers to parent table video on videoid: optional (may be NULL or dangling).
- Child table name_celebrity on celebrityid refers to parent table celebrity on celebrityid: required.
- Child table name_celebrity on nameid refers to parent table name on nameid: required.
- Child table picture_name on nameid refers to parent table name on nameid: required.
- Child table picture_name on pictureid refers to parent table picture on pictureid: required.
- Child table playlist_name on nameid refers to parent table name on nameid: required.
- Child table playlist_name on playlistid refers to parent table playlist on playlistid: required.
- Child table playlistitem on galerieid refers to parent table galerie on galerieid: optional (may be NULL or dangling).
- Child table playlistitem on playlistid refers to parent table playlist on playlistid: required.
- Child table playlistitem on videoid refers to parent table video on videoid: optional (may be NULL or dangling).
- Child table regisseur on celebrityid refers to parent table celebrity on celebrityid: required.
- Child table regisseur on videoid refers to parent table video on videoid: required.
- Child table snippet on sourcevideoid refers to parent table video on videoid: required.
- Child table snippet on videoid refers to parent table video on videoid: required.
- Child table tag_galerie on galerieid refers to parent table galerie on galerieid: required.
- Child table tag_galerie on tagid refers to parent table tag on tagid: required.
- Child table tag_group on groupid refers to parent table group on groupid: required.
- Child table tag_group on tagid refers to parent table tag on tagid: required.
- Child table tag_name on nameid refers to parent table name on nameid: required.
- Child table tag_name on tagid refers to parent table tag on tagid: required.
- Child table tag_picture on pictureid refers to parent table picture on pictureid: required.
- Child table tag_picture on tagid refers to parent table tag on tagid: required.
- Child table tag_playlist on playlistid refers to parent table playlist on playlistid: required.
- Child table tag_playlist on tagid refers to parent table tag on tagid: required.
- Child table tag_video on tagid refers to parent table tag on tagid: required.
- Child table tag_video on videoid refers to parent table video on videoid: required.
- Child table user_group on groupid refers to parent table group on groupid: required.
- Child table user_group on userid refers to parent table user on userid: required.
- Child table variation_name on nameid refers to parent table name on nameid: required.
- Child table variation_name on variationid refers to parent table variation on variationid: required.
- Child table video on thumbnailpictureid refers to parent table picture on pictureid: optional (may be NULL or dangling).
- Child table video_name on nameid refers to parent table name on nameid: required.
- Child table video_name on videoid refers to parent table video on videoid: required.
- Child table video_variation on variationid refers to parent table variation on variationid: required.
- Child table video_variation on videoid refers to parent table video on videoid: required.

MART picture_video_distribution — Per-(picture, measure state) distribution of linked video activity in the 079208_leavideo.ddl schema.

Grain: one row per (pictureid, measure state) pair represented by linked video rows, plus one absent no-activity row for a picture row with no links. Because lengthseconds is required, no linked video row belongs to the absent state.

Key columns: entity_key and measure_state together identify one output row.

Output columns:
- entity_key (integer): identifier of the picture row.
- measure_state (text): 'present' for a linked video row; 'absent' only for a picture row with no linked video row. lengthseconds is required on every real video row.
- entity_name (text): format of the picture row, copied unchanged.
- row_count (bigint): number of linked video rows in this entity/state cell; 0 for a no-activity absent cell.
- distinct_amount_count (bigint): number of unique lengthseconds values in this cell; each unique value is counted once, however many rows repeat it; 0 for a no-activity absent cell.
- total_amount (integer): total of lengthseconds in this cell; 0 for a no-activity absent cell.
- max_amount (integer): largest lengthseconds in this cell; 0 for a no-activity absent cell.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rules that build picture_video_distribution:

1. Source table picture is read in full from its backend, and every picture row is available to the work below.

2. Source table video is read in full from its backend, and every video row is available to the work below.

3. From source table picture, each pictureid is carried into the measure-state calculation as entity_key and its format is carried as entity_name; both entity_key and entity_name travel with the picture entity through the rest of the work.

4. The linked video rows are brought into each picture entity, matching a video row to a picture entity when that video row's thumbnailpictureid equals the picture entity's entity_key, and preservation is left-sided on the picture side: an entity with no linked row from source table video is retained as a placeholder so its absent state is visible, carrying entity_key, entity_name, pictureid and thumbnailpictureid.

5. The present measure-state rows are the ones kept where a real video row was matched; lengthseconds is required on every such row, so every matched row qualifies. These kept rows carry entity_key and entity_name.

6. There is one row for each picture entity that has at least one row in the present measure state, and no row here for an entity with none; each such entity/state cell, identified by entity_key with its entity_name, reports row_count (how many linked video rows it has), distinct_amount_count (how many different lengthseconds values occur, each different value counted once, however many rows repeat it), total_amount (the total of lengthseconds) and max_amount (the largest lengthseconds).

7. For each of these present-state cells, max_amount_share is max_amount divided by total_amount expressed as a fraction, rounded to 4 decimal places, and is 0.0 when total_amount is 0; the cell carries entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

8. These measures are labelled as the present measure state, so measure_state reads 'present' on each such row, which carries entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

9. The absent measure-state rows are the retained placeholders for a picture row with no video rows; no real row can enter this state because lengthseconds is required on every real video row. These kept rows carry entity_key and entity_name.

10. There is one row for each picture entity with no linked video row at all, whose retained placeholder is its one row in the absent measure state, and no row here for an entity that has a linked video row; each such cell, identified by entity_key with its entity_name, reports a row_count of 0, a distinct_amount_count of 0 different lengthseconds values, a total_amount of lengthseconds of 0 and a max_amount, the largest lengthseconds, of 0.

11. For each of these absent-state cells, max_amount_share is max_amount divided by total_amount expressed as a fraction, rounded to 4 decimal places, and is 0.0 when total_amount is 0; the cell carries entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

12. These measures are labelled as the absent measure state, so measure_state reads 'absent' on each such row, which carries entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

13. The present-state summary and the absent-state summary are stacked into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other; each output row carries entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

14. The output order is deterministic: rows appear in ascending entity_key order, and within one entity_key in ascending measure_state order.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `picture_video_distribution`

- Grain: One row per (pictureid, measure state) pair represented by linked video rows, plus one absent no-activity row for a picture row with no links. Because lengthseconds is required, no linked video row belongs to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'picture_video_distribution' has 14 declared semantic rules:
1. [source] Read source table picture. (public source tables: picture)
2. [source] Read source table video. (public source tables: video)
3. [derive] Carry each pictureid and its format into the measure-state calculation. (public source tables: picture | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked video rows into each picture entity; retain an entity with no linked row so its absent state is visible. (public source tables: video | public carried/output columns: entity_key, entity_name, pictureid, thumbnailpictureid | join preservation: left | condition public identifiers: video, thumbnailpictureid, entity_key)
5. [filter] Keep the present measure-state rows: a real video row; lengthseconds is required on every such row. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per picture entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different lengthseconds values occur (each different value counted once, however many rows repeat it), total lengthseconds, and largest lengthseconds. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: the retained placeholder for a picture row with no video rows; no real row can enter this state because lengthseconds is required. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per picture entity with no linked video row at all, whose retained placeholder is its one row in the absent measure state, and no row here for an entity that has a linked video row, reporting a row count of 0, 0 different lengthseconds values, a total lengthseconds of 0 and a largest lengthseconds of 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

## Source tables

### actor  (source backend: files)
Source table Actor.

- `celebrityid`: integer NOT NULL — Column celebrityID of table Actor.
- `videoid`: integer NOT NULL — Column videoID of table Actor.
- primary key: celebrityid, videoid

### artist  (source backend: s3)
Source table Artist.

- `celebrityid`: integer NOT NULL — Column celebrityID of table Artist.
- `videoid`: integer NOT NULL — Column videoID of table Artist.
- primary key: celebrityid, videoid

### celebrity  (source backend: s3)
Source table Celebrity.

- `avatarpictureid`: integer NULL — Column avatarPictureID of table Celebrity.
- `celebrityid`: integer NOT NULL — Column celebrityID of table Celebrity.
- primary key: celebrityid

### celebrity_picture  (source backend: mongodb)
Source table Celebrity_Picture.

- `celebrityid`: integer NOT NULL — Column celebrityID of table Celebrity_Picture.
- `pictureid`: integer NOT NULL — Column pictureID of table Celebrity_Picture.
- primary key: celebrityid, pictureid

### celebrity_tag  (source backend: s3)
Source table Celebrity_Tag.

- `celebrityid`: integer NOT NULL — Column celebrityID of table Celebrity_Tag.
- `tagid`: integer NOT NULL — Column tagID of table Celebrity_Tag.
- primary key: celebrityid, tagid

### galerie  (source backend: s3)
Source table Galerie.

- `galerieid`: integer NOT NULL — Column galerieID of table Galerie.
- primary key: galerieid

### galerie_name  (source backend: s3)
Source table Galerie_Name.

- `galerieid`: integer NOT NULL — Column galerieID of table Galerie_Name.
- `nameid`: integer NOT NULL — Column nameID of table Galerie_Name.
- primary key: galerieid, nameid

### galerie_picture  (source backend: mongodb)
Source table Galerie_Picture.

- `galerieid`: integer NOT NULL — Column galerieID of table Galerie_Picture.
- `pictureid`: integer NOT NULL — Column pictureID of table Galerie_Picture.
- primary key: galerieid, pictureid

### group  (source backend: files)
Source table Group.

- `groupid`: integer NOT NULL — Column groupID of table Group.
- `parentgroupid`: integer NOT NULL — Column parentGroupID of table Group.
- primary key: groupid

### media  (source backend: postgres)
Source table Media.

- `videovideoid`: integer NOT NULL — Column VideovideoID of table Media.
- `audiobitrate`: integer NOT NULL — Column audioBitrate of table Media.
- `audioencoding`: text NOT NULL — Column audioEncoding of table Media.
- `hasaudio`: integer NOT NULL — Column hasAudio of table Media.
- `hasvideo`: integer NOT NULL — Column hasVideo of table Media.
- `mediaid`: integer NOT NULL — Column mediaID of table Media.
- `videobitrate`: integer NOT NULL — Column videoBitrate of table Media.
- `videoencoding`: text NOT NULL — Column videoEncoding of table Media.
- primary key: mediaid

### movie  (source backend: files)
Source table Movie.

- `imdbid`: text NOT NULL — Column imdbID of table Movie.
- `videoid`: integer NULL — Column videoID of table Movie.

### name  (source backend: files)
Source table Name.

- `language`: text NULL — Column language of table Name.
- `name`: text NULL — Column name of table Name.
- `nameid`: integer NOT NULL — Column nameID of table Name.
- primary key: nameid

### name_celebrity  (source backend: postgres)
Source table Name_Celebrity.

- `celebrityid`: integer NOT NULL — Column celebrityID of table Name_Celebrity.
- `nameid`: integer NOT NULL — Column nameID of table Name_Celebrity.
- primary key: celebrityid, nameid

### picture  (source backend: postgres)
Source table Picture.

- `format`: text NULL — Column format of table Picture.
- `height`: integer NULL — Column height of table Picture.
- `pictureid`: integer NOT NULL — Column pictureID of table Picture.
- `width`: integer NULL — Column width of table Picture.
- primary key: pictureid

### picture_name  (source backend: mongodb)
Source table Picture_Name.

- `nameid`: integer NOT NULL — Column nameID of table Picture_Name.
- `pictureid`: integer NOT NULL — Column pictureID of table Picture_Name.
- primary key: nameid, pictureid

### playlist  (source backend: postgres)
Source table Playlist.

- `playlistid`: integer NOT NULL — Column playlistID of table Playlist.
- primary key: playlistid

### playlistitem  (source backend: files)
Source table PlaylistItem.

- `galerieid`: integer NULL — Column galerieID of table PlaylistItem.
- `playlistid`: integer NOT NULL — Column playlistID of table PlaylistItem.
- `playlistitemid`: integer NOT NULL — Column playlistItemID of table PlaylistItem.
- `videoid`: integer NULL — Column videoID of table PlaylistItem.
- primary key: playlistitemid

### playlist_name  (source backend: s3)
Source table Playlist_Name.

- `nameid`: integer NOT NULL — Column nameID of table Playlist_Name.
- `playlistid`: integer NOT NULL — Column playlistID of table Playlist_Name.
- primary key: nameid, playlistid

### regisseur  (source backend: rest)
Source table Regisseur.

- `celebrityid`: integer NOT NULL — Column celebrityID of table Regisseur.
- `videoid`: integer NOT NULL — Column videoID of table Regisseur.
- primary key: celebrityid, videoid

### snippet  (source backend: rest)
Source table Snippet.

- `endframe`: integer NOT NULL — Column endFrame of table Snippet.
- `sourcevideoid`: integer NOT NULL — Column sourceVideoID of table Snippet.
- `startframe`: integer NOT NULL — Column startFrame of table Snippet.
- `videoid`: integer NOT NULL — Column videoID of table Snippet.

### tag  (source backend: mongodb)
Source table Tag.

- `defaulttag`: integer NULL — Column defaultTag of table Tag.
- `parenttagid`: integer NULL — Column parentTagID of table Tag.
- `tagid`: integer NOT NULL — Column tagID of table Tag.
- primary key: tagid

### tag_galerie  (source backend: postgres)
Source table Tag_Galerie.

- `galerieid`: integer NOT NULL — Column galerieID of table Tag_Galerie.
- `tagid`: integer NOT NULL — Column tagID of table Tag_Galerie.
- primary key: galerieid, tagid

### tag_group  (source backend: mongodb)
Source table Tag_Group.

- `groupid`: integer NOT NULL — Column groupID of table Tag_Group.
- `tagid`: integer NOT NULL — Column tagID of table Tag_Group.
- primary key: groupid, tagid

### tag_name  (source backend: rest)
Source table Tag_Name.

- `nameid`: integer NOT NULL — Column nameID of table Tag_Name.
- `tagid`: integer NOT NULL — Column tagID of table Tag_Name.
- primary key: nameid, tagid

### tag_picture  (source backend: postgres)
Source table Tag_Picture.

- `pictureid`: integer NOT NULL — Column pictureID of table Tag_Picture.
- `tagid`: integer NOT NULL — Column tagID of table Tag_Picture.
- primary key: pictureid, tagid

### tag_playlist  (source backend: rest)
Source table Tag_Playlist.

- `playlistid`: integer NOT NULL — Column playlistID of table Tag_Playlist.
- `tagid`: integer NOT NULL — Column tagID of table Tag_Playlist.
- primary key: playlistid, tagid

### tag_video  (source backend: postgres)
Source table Tag_Video.

- `tagid`: integer NOT NULL — Column tagID of table Tag_Video.
- `videoid`: integer NOT NULL — Column videoID of table Tag_Video.
- primary key: tagid, videoid

### user  (source backend: postgres)
Source table User.

- `password`: text NULL — Column password of table User.
- `salt`: text NULL — Column salt of table User.
- `userid`: integer NOT NULL — Column userID of table User.
- primary key: userid

### user_group  (source backend: s3)
Source table User_Group.

- `groupid`: integer NOT NULL — Column groupID of table User_Group.
- `userid`: integer NOT NULL — Column userID of table User_Group.
- primary key: groupid, userid

### variation  (source backend: files)
Source table Variation.

- `variationid`: integer NOT NULL — Column variationID of table Variation.
- primary key: variationid

### variation_name  (source backend: s3)
Source table Variation_Name.

- `nameid`: integer NOT NULL — Column nameID of table Variation_Name.
- `variationid`: integer NOT NULL — Column variationID of table Variation_Name.
- primary key: nameid, variationid

### video  (source backend: postgres)
Source table Video.

- `lengthseconds`: integer NOT NULL — Column lengthSeconds of table Video.
- `thumbnailpictureid`: integer NULL — Column thumbnailPictureID of table Video.
- `videoid`: integer NOT NULL — Column videoID of table Video.
- primary key: videoid

### video_name  (source backend: rest)
Source table Video_Name.

- `nameid`: integer NOT NULL — Column nameID of table Video_Name.
- `videoid`: integer NOT NULL — Column videoID of table Video_Name.
- primary key: nameid, videoid

### video_variation  (source backend: s3)
Source table Video_Variation.

- `isdefaultaudio`: integer NOT NULL — Column isDefaultAudio of table Video_Variation.
- `isdefaultvideo`: integer NOT NULL — Column isDefaultVideo of table Video_Variation.
- `variationid`: integer NOT NULL — Column variationID of table Video_Variation.
- `videoid`: integer NOT NULL — Column videoID of table Video_Variation.
- primary key: variationid, videoid

### Relationships

- actor(celebrityid) -> celebrity(celebrityid) [required]
- actor(videoid) -> video(videoid) [required]
- artist(celebrityid) -> celebrity(celebrityid) [required]
- artist(videoid) -> video(videoid) [required]
- celebrity(avatarpictureid) -> picture(pictureid) [optional (may be NULL/dangling)]
- celebrity_picture(celebrityid) -> celebrity(celebrityid) [required]
- celebrity_picture(pictureid) -> picture(pictureid) [required]
- celebrity_tag(celebrityid) -> celebrity(celebrityid) [required]
- celebrity_tag(tagid) -> tag(tagid) [required]
- galerie_name(galerieid) -> galerie(galerieid) [required]
- galerie_name(nameid) -> name(nameid) [required]
- galerie_picture(galerieid) -> galerie(galerieid) [required]
- galerie_picture(pictureid) -> picture(pictureid) [required]
- media(videovideoid) -> video(videoid) [required]
- movie(videoid) -> video(videoid) [optional (may be NULL/dangling)]
- name_celebrity(celebrityid) -> celebrity(celebrityid) [required]
- name_celebrity(nameid) -> name(nameid) [required]
- picture_name(nameid) -> name(nameid) [required]
- picture_name(pictureid) -> picture(pictureid) [required]
- playlist_name(nameid) -> name(nameid) [required]
- playlist_name(playlistid) -> playlist(playlistid) [required]
- playlistitem(galerieid) -> galerie(galerieid) [optional (may be NULL/dangling)]
- playlistitem(playlistid) -> playlist(playlistid) [required]
- playlistitem(videoid) -> video(videoid) [optional (may be NULL/dangling)]
- regisseur(celebrityid) -> celebrity(celebrityid) [required]
- regisseur(videoid) -> video(videoid) [required]
- snippet(sourcevideoid) -> video(videoid) [required]
- snippet(videoid) -> video(videoid) [required]
- tag_galerie(galerieid) -> galerie(galerieid) [required]
- tag_galerie(tagid) -> tag(tagid) [required]
- tag_group(groupid) -> group(groupid) [required]
- tag_group(tagid) -> tag(tagid) [required]
- tag_name(nameid) -> name(nameid) [required]
- tag_name(tagid) -> tag(tagid) [required]
- tag_picture(pictureid) -> picture(pictureid) [required]
- tag_picture(tagid) -> tag(tagid) [required]
- tag_playlist(playlistid) -> playlist(playlistid) [required]
- tag_playlist(tagid) -> tag(tagid) [required]
- tag_video(tagid) -> tag(tagid) [required]
- tag_video(videoid) -> video(videoid) [required]
- user_group(groupid) -> group(groupid) [required]
- user_group(userid) -> user(userid) [required]
- variation_name(nameid) -> name(nameid) [required]
- variation_name(variationid) -> variation(variationid) [required]
- video(thumbnailpictureid) -> picture(pictureid) [optional (may be NULL/dangling)]
- video_name(nameid) -> name(nameid) [required]
- video_name(videoid) -> video(videoid) [required]
- video_variation(variationid) -> variation(variationid) [required]
- video_variation(videoid) -> video(videoid) [required]

