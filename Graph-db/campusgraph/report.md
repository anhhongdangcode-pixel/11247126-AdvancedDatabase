# CampusGraph Lab Report

## 1. Relational Schema & SQL Recursive Query for Task 2

### Relational Schema (SQL)
```sql
CREATE TABLE courses (
    code    VARCHAR(10) PRIMARY KEY,
    title   VARCHAR(100) NOT NULL,
    credits INT NOT NULL
);

CREATE TABLE requires (
    course_code VARCHAR(10) REFERENCES courses(code),
    prereq_code VARCHAR(10) REFERENCES courses(code),
    PRIMARY KEY (course_code, prereq_code)
);

CREATE TABLE students (
    id   VARCHAR(10) PRIMARY KEY,
    name VARCHAR(50) NOT NULL,
    year INT NOT NULL
);

CREATE TABLE passed (
    student_id  VARCHAR(10) REFERENCES students(id),
    course_code VARCHAR(10) REFERENCES courses(code),
    grade       NUMERIC(3, 1) NOT NULL,
    PRIMARY KEY (student_id, course_code)
);

CREATE TABLE friends (
    student_a VARCHAR(10) REFERENCES students(id),
    student_b VARCHAR(10) REFERENCES students(id),
    PRIMARY KEY (student_a, student_b),
    CHECK (student_a < student_b)
);
```

### Task 2 in SQL (`WITH RECURSIVE`) vs. Cypher

**SQL (`WITH RECURSIVE`):**
```sql
WITH RECURSIVE prereq_chain(code, depth) AS (
    SELECT prereq_code, 1
    FROM requires
    WHERE course_code = :code
    UNION ALL
    SELECT r.prereq_code, pc.depth + 1
    FROM prereq_chain pc
    JOIN requires r ON r.course_code = pc.code
)
SELECT code, MIN(depth) AS depth
FROM prereq_chain
GROUP BY code
ORDER BY depth ASC, code ASC;
```

**Cypher:**
```cypher
MATCH path = (c:Course {code: $code})-[:REQUIRES*1..]->(pre:Course)
RETURN pre.code AS code, min(length(path)) AS depth
ORDER BY depth ASC, code ASC
```

**Comparison:** The Cypher query is 3 lines compared to 13 lines of SQL. Cypher expresses variable-length traversal natively (`-[:REQUIRES*1..]->`) and visually mirrors the graph structure, whereas SQL requires explicit recursive CTE scaffolding (base query, `UNION ALL`, self-join on a junction table, manual depth counter, and explicit `GROUP BY`).

---

## 2. Path Multiplicity & Aggregation in Task 2

In Cypher, `MATCH path = (c:Course {code: $code})-[:REQUIRES*1..]->(pre:Course)` returns **one row per distinct path** matching the pattern. When a prerequisite is reachable via multiple routes, it appears multiple times (once per path). Only when an aggregation function (`min(length(path))`) is introduced in `RETURN` does Cypher implicitly group rows by the non-aggregated key (`pre.code`), collapsing duplicates into a single row per prerequisite with its shortest hop count.

Running the query for `CAP401` to `CS101` without aggregation returns **2 paths**:
1. `CAP401 -> ML201 -> CS102 -> CS101` (length **3**)
2. `CAP401 -> DB301 -> DB201 -> CS102 -> CS101` (length **4**)

After aggregation, `min(length(path))` collapses both rows into `("CS101", 3)`.

---

## 3. Query Plan & Indexing Impact (Step 8)

Profiling the Task 3 query (`eligible_courses("S01")`) on Neo4j with and without the unique constraint `student_id`:

| Configuration | First Operator (for `:Student`) | Operator Details & Hits | Total DB Hits |
| :--- | :--- | :--- | ---: |
| **With `student_id` constraint** | `NodeUniqueIndexSeek` | `UNIQUE s:Student(id) WHERE id = $sid` (2 db hits, 1 row) | **201** |
| **Without `student_id` constraint** | `NodeByLabelScan` → `Filter` | Scan all 10 `:Student` nodes (11 db hits) + filter `s.id = $sid` (10 db hits) | **220** |

**Scaling to 100,000 students:**
- **With constraint (unique index):** `NodeUniqueIndexSeek` operates in $O(\log N)$ time—locating one student still takes only **~2–4 db hits**, keeping query latency sub-millisecond.
- **Without constraint:** `NodeByLabelScan` + `Filter` operates in $O(N)$ time—it must scan all 100,000 `:Student` nodes (~100,001 db hits) and read `s.id` on every node (+100,000 db hits), adding **~200,000 wasted db hits** per query just to find a single student.

---

## 4. Storing `FRIEND` in One Direction vs. Both Directions

**Why store `FRIEND` once:** Friendship is symmetric. While Neo4j requires every stored relationship to have a physical direction, Cypher supports direction-agnostic traversal (`-[:FRIEND]-`). Storing a single relationship halves storage and adjacency-list size, and eliminates update anomalies (e.g., deleting `(a)-[:FRIEND]->(b)` without deleting `(b)-[:FRIEND]->(a)`).

**Impact of storing both directions while keeping the same undirected queries:**
- **Task 4 (`suggest_friends`):** Because `NOT (s)-[:FRIEND]-(other)` is a boolean existence predicate, the output remains identical (`[("Minh", 4), ("Phuong", 4), ("Chi", 3)]`), **but** Neo4j inspects twice as many `:FRIEND` edges during checks (measured: **327 → 377 db hits**). Furthermore, any query that *expands* `-[:FRIEND]-` (e.g., counting friends or traversing friends-of-friends) would match every friendship twice and **double-count results**.
- **Task 5 (`connection`):** `shortestPath((a)-[:FRIEND*0..]-(b))` still returns one path, but every node's `:FRIEND` degree doubles, increasing BFS work (**31 → 37 db hits** even on 10 nodes). Worse, if a query used `allShortestPaths` or unaggregated variable-length matching, a path of length $L$ would explode into **$2^L$ duplicate paths** (verified on `S01` → `S08`, length 3: `allShortestPaths` jumped from **1** to **$2^3 = 8$** paths).

---

## 5. Model Redesign for Course Retakes Across Semesters

A direct `[:PASSED]` relationship with a `grade` property cannot cleanly represent multiple attempts across semesters (including failed attempts and retakes), nor attach semester/section metadata without creating ambiguous parallel edges.

### Redesigned Model (`:Enrollment` Node)
Reify each attempt as an **`:Enrollment`** node holding `semester`, `grade`, and `status` (`"PASSED"` or `"FAILED"`):

```
(s:Student {id, name, year})
      │
      ▼  [:HAS_ENROLLMENT]
(e:Enrollment {semester, grade, status})
      │
      ▼  [:FOR_COURSE]
(c:Course {code, title, credits})
```

### Rewritten Task 1 Query
To preserve Task 1's output `[(code, grade), ...]` (using the student's best passing grade if they retook a passed course):

```cypher
MATCH (s:Student {id: $sid})-[:HAS_ENROLLMENT]->(e:Enrollment)-[:FOR_COURSE]->(c:Course)
WHERE e.status = "PASSED"
RETURN c.code AS code, max(e.grade) AS grade
ORDER BY grade DESC, code ASC
```

---

## 6. Concurrency Anomaly in Task 7 Without a Single Transaction

If the prerequisite check and the `MERGE` write ran in **two separate `execute_query` calls**, each call would execute in its own auto-commit transaction, creating a **Time-of-Check to Time-of-Use (TOCTOU) race condition**:

1. **Transaction 1 (Check):** Checks prerequisites for `S08` taking `CS102`. Sees `(S08)-[:PASSED]->(CS101)` exists → validation succeeds, transaction 1 finishes.
2. **Concurrent Transaction (External program):** Deletes `(S08)-[:PASSED]->(CS101)` (e.g., grade revocation) and commits.
3. **Transaction 2 (Write):** Blindly creates `(S08)-[:PASSED {grade: 7.0}]->(CS102)` without re-checking.

**Result:** The database enters an **inconsistent state** where `S08` has passed `CS102` without having passed its required prerequisite `CS101`. Wrapping validation and write inside a single write transaction (`session.execute_write`) keeps the operation atomic and ensures any failure rolls back cleanly (and allows locking the `:Student` node within the same transaction if strict serialization against concurrent deletes is needed under Read Committed isolation).
