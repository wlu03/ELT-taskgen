-- deterministic load script for customers (elt-taskgen)
DROP TABLE IF EXISTS "customers";
CREATE TABLE "customers" (
  "customer_id" INTEGER NOT NULL,
  "customer_name" TEXT NOT NULL,
  "segment" TEXT,
  PRIMARY KEY ("customer_id")
);
INSERT INTO "customers" ("customer_id", "customer_name", "segment") VALUES
(10001, 'Ada', 'gold'),
(10002, 'Bob', NULL),
(10003, 'Cy', 'null'),
(10004, 'Di', ''),
(10005, 'Ed', 'None');
