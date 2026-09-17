-- deterministic load script for customers (elt-taskgen)
DROP TABLE IF EXISTS "customers";
CREATE TABLE "customers" (
  "customer_id" INTEGER NOT NULL,
  "customer_name" TEXT NOT NULL,
  "segment" TEXT,
  PRIMARY KEY ("customer_id")
);
INSERT INTO "customers" ("customer_id", "customer_name", "segment") VALUES
(101, 'Ada', 'gold'),
(102, 'Bob', NULL),
(103, 'Cy', 'null'),
(104, 'Di', ''),
(105, 'Ed', 'None');
