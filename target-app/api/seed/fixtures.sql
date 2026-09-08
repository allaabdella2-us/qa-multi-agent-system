-- fixtures.sql — deterministic demo data for the Corvid Orders API.
--
-- Row counts after this file has been applied to a fresh schema:
--   organizations  2
--   users          4
--   orders         35   (org 1 Northwind: 30, org 2 Contoso: 5)
--   order_items    67   (org 1 Northwind: 60, org 2 Contoso: 7)
--   invoices       7    (org 1 Northwind: 6, org 2 Contoso: 1)
--
-- Northwind orders cycle draft/placed/paid/refunded/cancelled, 6 of each.
-- Every account signs in with the password 'password123'.

BEGIN;

INSERT INTO organizations (id, name, created_at) VALUES
    (1, 'Northwind', '2024-11-04 08:00:00+00'),
    (2, 'Contoso',   '2024-12-19 14:30:00+00');

INSERT INTO users (id, org_id, email, password_hash, role) VALUES
    (1, 1, 'admin@northwind.test', 'pbkdf2_sha256$260000$a1b2c3d4e5f60718293a4b5c6d7e8f90$cb08a0be2e4ff5d535e6cb4a84fa207cf9f0fd7b89448f68da18b7129299c543', 'admin'),
    (2, 1, 'member@northwind.test', 'pbkdf2_sha256$260000$b2c3d4e5f60718293a4b5c6d7e8f90a1$76560b1c3ba3e4b6b684885f4cb33b84279f74b28d5ff36b37d265e3ed09d546', 'member'),
    (3, 1, 'viewer@northwind.test', 'pbkdf2_sha256$260000$c3d4e5f60718293a4b5c6d7e8f90a1b2$99bd40b66786dbae31bd02f549686b93e4ea94cd132bd62cbc75daf0481d285e', 'viewer'),
    (4, 2, 'admin@contoso.test', 'pbkdf2_sha256$260000$d4e5f60718293a4b5c6d7e8f90a1b2c3$2f95d0ce1c867b32a78cf3440a28e2dcb24b80349fc1a3a460d0a43e07f64764', 'admin');

INSERT INTO orders (id, org_id, reference, status, total_cents, currency, created_at) VALUES
    (1, 1, 'NW-1001', 'draft', 2850, 'USD', '2025-01-06 09:15:00+00'),
    (2, 1, 'NW-1002', 'placed', 10300, 'USD', '2025-01-07 02:15:00+00'),
    (3, 1, 'NW-1003', 'paid', 11300, 'USD', '2025-01-07 19:15:00+00'),
    (4, 1, 'NW-1004', 'refunded', 1950, 'USD', '2025-01-08 12:15:00+00'),
    (5, 1, 'NW-1005', 'cancelled', 9950, 'USD', '2025-01-09 05:15:00+00'),
    (6, 1, 'NW-1006', 'draft', 14050, 'USD', '2025-01-09 22:15:00+00'),
    (7, 1, 'NW-1007', 'placed', 5900, 'USD', '2025-01-10 15:15:00+00'),
    (8, 1, 'NW-1008', 'paid', 4500, 'USD', '2025-01-11 08:15:00+00'),
    (9, 1, 'NW-1009', 'refunded', 13950, 'USD', '2025-01-12 01:15:00+00'),
    (10, 1, 'NW-1010', 'cancelled', 6000, 'USD', '2025-01-12 18:15:00+00'),
    (11, 1, 'NW-1011', 'draft', 10650, 'USD', '2025-01-13 11:15:00+00'),
    (12, 1, 'NW-1012', 'placed', 11300, 'USD', '2025-01-14 04:15:00+00'),
    (13, 1, 'NW-1013', 'paid', 3050, 'USD', '2025-01-14 21:15:00+00'),
    (14, 1, 'NW-1014', 'refunded', 11000, 'USD', '2025-01-15 14:15:00+00'),
    (15, 1, 'NW-1015', 'cancelled', 12000, 'USD', '2025-01-16 07:15:00+00'),
    (16, 1, 'NW-1016', 'draft', 2050, 'USD', '2025-01-17 00:15:00+00'),
    (17, 1, 'NW-1017', 'placed', 10450, 'USD', '2025-01-17 17:15:00+00'),
    (18, 1, 'NW-1018', 'paid', 14850, 'USD', '2025-01-18 10:15:00+00'),
    (19, 1, 'NW-1019', 'refunded', 6300, 'USD', '2025-01-19 03:15:00+00'),
    (20, 1, 'NW-1020', 'cancelled', 4800, 'USD', '2025-01-19 20:15:00+00'),
    (21, 1, 'NW-1021', 'draft', 14850, 'USD', '2025-01-20 13:15:00+00'),
    (22, 1, 'NW-1022', 'placed', 6300, 'USD', '2025-01-21 06:15:00+00'),
    (23, 1, 'NW-1023', 'paid', 7150, 'USD', '2025-01-21 23:15:00+00'),
    (24, 1, 'NW-1024', 'refunded', 11900, 'USD', '2025-01-22 16:15:00+00'),
    (25, 1, 'NW-1025', 'cancelled', 3250, 'USD', '2025-01-23 09:15:00+00'),
    (26, 1, 'NW-1026', 'draft', 11700, 'USD', '2025-01-24 02:15:00+00'),
    (27, 1, 'NW-1027', 'placed', 12700, 'USD', '2025-01-24 19:15:00+00'),
    (28, 1, 'NW-1028', 'paid', 2150, 'USD', '2025-01-25 12:15:00+00'),
    (29, 1, 'NW-1029', 'refunded', 8950, 'USD', '2025-01-26 05:15:00+00'),
    (30, 1, 'NW-1030', 'cancelled', 11650, 'USD', '2025-01-26 22:15:00+00'),
    (31, 2, 'CT-2001', 'draft', 6700, 'EUR', '2025-01-09 09:15:00+00'),
    (32, 2, 'CT-2002', 'placed', 5100, 'EUR', '2025-01-09 20:15:00+00'),
    (33, 2, 'CT-2003', 'paid', 4050, 'EUR', '2025-01-10 07:15:00+00'),
    (34, 2, 'CT-2004', 'refunded', 14500, 'EUR', '2025-01-10 18:15:00+00'),
    (35, 2, 'CT-2005', 'cancelled', 5500, 'EUR', '2025-01-11 05:15:00+00');

INSERT INTO order_items (id, order_id, sku, description, quantity, unit_price_cents) VALUES
    (1, 1, 'SKU-CP-107', 'Corvid ringing pliers', 2, 1425),
    (2, 2, 'SKU-LB-114', 'Weatherproof leg bands', 3, 1600),
    (3, 2, 'SKU-SM-127', 'Seed mix, 5kg', 4, 1375),
    (4, 3, 'SKU-DL-121', 'Data logger, 90 day', 4, 1775),
    (5, 3, 'SKU-RF-134', 'Rook feeder, galvanised', 1, 1550),
    (6, 3, 'SKU-JN-147', 'Jackdaw nesting box', 2, 1325),
    (7, 4, 'SKU-MD-128', 'Magpie deterrent kit', 1, 1950),
    (8, 5, 'SKU-OB-135', 'Observation blind, 2m', 2, 2125),
    (9, 5, 'SKU-LB-148', 'Weatherproof leg bands', 3, 1900),
    (10, 6, 'SKU-PD-142', 'Perch dowel, oak', 3, 1300),
    (11, 6, 'SKU-DL-155', 'Data logger, 90 day', 4, 2075),
    (12, 6, 'SKU-RF-168', 'Rook feeder, galvanised', 1, 1850),
    (13, 7, 'SKU-JN-149', 'Jackdaw nesting box', 4, 1475),
    (14, 8, 'SKU-FN-156', 'Field notebook, waxed', 1, 1650),
    (15, 8, 'SKU-OB-169', 'Observation blind, 2m', 2, 1425),
    (16, 9, 'SKU-SM-163', 'Seed mix, 5kg', 2, 1825),
    (17, 9, 'SKU-PD-176', 'Perch dowel, oak', 3, 1600),
    (18, 9, 'SKU-DL-189', 'Data logger, 90 day', 4, 1375),
    (19, 10, 'SKU-RF-170', 'Rook feeder, galvanised', 3, 2000),
    (20, 11, 'SKU-CP-177', 'Corvid ringing pliers', 4, 2175),
    (21, 11, 'SKU-FN-190', 'Field notebook, waxed', 1, 1950),
    (22, 12, 'SKU-LB-184', 'Weatherproof leg bands', 1, 1350),
    (23, 12, 'SKU-SM-197', 'Seed mix, 5kg', 2, 2125),
    (24, 12, 'SKU-PD-210', 'Perch dowel, oak', 3, 1900),
    (25, 13, 'SKU-DL-191', 'Data logger, 90 day', 2, 1525),
    (26, 14, 'SKU-MD-198', 'Magpie deterrent kit', 3, 1700),
    (27, 14, 'SKU-CP-211', 'Corvid ringing pliers', 4, 1475),
    (28, 15, 'SKU-OB-205', 'Observation blind, 2m', 4, 1875),
    (29, 15, 'SKU-LB-218', 'Weatherproof leg bands', 1, 1650),
    (30, 15, 'SKU-SM-231', 'Seed mix, 5kg', 2, 1425),
    (31, 16, 'SKU-PD-212', 'Perch dowel, oak', 1, 2050),
    (32, 17, 'SKU-JN-219', 'Jackdaw nesting box', 2, 2225),
    (33, 17, 'SKU-MD-232', 'Magpie deterrent kit', 3, 2000),
    (34, 18, 'SKU-FN-226', 'Field notebook, waxed', 3, 1400),
    (35, 18, 'SKU-OB-239', 'Observation blind, 2m', 4, 2175),
    (36, 18, 'SKU-LB-252', 'Weatherproof leg bands', 1, 1950),
    (37, 19, 'SKU-SM-233', 'Seed mix, 5kg', 4, 1575),
    (38, 20, 'SKU-RF-240', 'Rook feeder, galvanised', 1, 1750),
    (39, 20, 'SKU-JN-253', 'Jackdaw nesting box', 2, 1525),
    (40, 21, 'SKU-CP-247', 'Corvid ringing pliers', 2, 1925),
    (41, 21, 'SKU-FN-260', 'Field notebook, waxed', 3, 1700),
    (42, 21, 'SKU-OB-273', 'Observation blind, 2m', 4, 1475),
    (43, 22, 'SKU-LB-254', 'Weatherproof leg bands', 3, 2100),
    (44, 23, 'SKU-DL-261', 'Data logger, 90 day', 4, 1275),
    (45, 23, 'SKU-RF-274', 'Rook feeder, galvanised', 1, 2050),
    (46, 24, 'SKU-MD-268', 'Magpie deterrent kit', 1, 1450),
    (47, 24, 'SKU-CP-281', 'Corvid ringing pliers', 2, 2225),
    (48, 24, 'SKU-FN-294', 'Field notebook, waxed', 3, 2000),
    (49, 25, 'SKU-OB-275', 'Observation blind, 2m', 2, 1625),
    (50, 26, 'SKU-PD-282', 'Perch dowel, oak', 3, 1800),
    (51, 26, 'SKU-DL-295', 'Data logger, 90 day', 4, 1575),
    (52, 27, 'SKU-JN-289', 'Jackdaw nesting box', 4, 1975),
    (53, 27, 'SKU-MD-302', 'Magpie deterrent kit', 1, 1750),
    (54, 27, 'SKU-CP-315', 'Corvid ringing pliers', 2, 1525),
    (55, 28, 'SKU-FN-296', 'Field notebook, waxed', 1, 2150),
    (56, 29, 'SKU-SM-303', 'Seed mix, 5kg', 2, 1325),
    (57, 29, 'SKU-PD-316', 'Perch dowel, oak', 3, 2100),
    (58, 30, 'SKU-RF-310', 'Rook feeder, galvanised', 3, 1500),
    (59, 30, 'SKU-JN-323', 'Jackdaw nesting box', 4, 1275),
    (60, 30, 'SKU-MD-336', 'Magpie deterrent kit', 1, 2050),
    (61, 31, 'SKU-CP-317', 'Corvid ringing pliers', 4, 1675),
    (62, 32, 'SKU-LB-324', 'Weatherproof leg bands', 1, 1850),
    (63, 32, 'SKU-SM-337', 'Seed mix, 5kg', 2, 1625),
    (64, 33, 'SKU-DL-331', 'Data logger, 90 day', 2, 2025),
    (65, 34, 'SKU-MD-338', 'Magpie deterrent kit', 3, 2200),
    (66, 34, 'SKU-CP-351', 'Corvid ringing pliers', 4, 1975),
    (67, 35, 'SKU-OB-345', 'Observation blind, 2m', 4, 1375);

INSERT INTO invoices (id, order_id, number, amount_cents, currency, issued_at, status) VALUES
    (1, 2, 'INV-2025-0001', 10300, 'USD', '2025-01-08 02:15:00+00', 'open'),
    (2, 3, 'INV-2025-0002', 11300, 'USD', '2025-01-08 19:15:00+00', 'paid'),
    (3, 5, 'INV-2025-0003', 9950, 'USD', '2025-01-10 05:15:00+00', 'void'),
    (4, 12, 'INV-2025-0004', 11300, 'USD', '2025-01-15 04:15:00+00', 'open'),
    (5, 13, 'INV-2025-0005', 3050, 'USD', '2025-01-15 21:15:00+00', 'paid'),
    (6, 23, 'INV-2025-0006', 7150, 'USD', '2025-01-22 23:15:00+00', 'paid'),
    (7, 33, 'INV-2025-1001', 4050, 'EUR', '2025-01-11 07:15:00+00', 'paid');

SELECT setval('organizations_id_seq', (SELECT max(id) FROM organizations));
SELECT setval('users_id_seq',         (SELECT max(id) FROM users));
SELECT setval('orders_id_seq',        (SELECT max(id) FROM orders));
SELECT setval('order_items_id_seq',   (SELECT max(id) FROM order_items));
SELECT setval('invoices_id_seq',      (SELECT max(id) FROM invoices));

COMMIT;
