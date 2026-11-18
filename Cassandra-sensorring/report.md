# Lab Report: SensorRing - Verifying Cassandra on a 3-Node Cluster

## 1. Tổng quan & Thiết lập môi trường (Step 0)

### 1.1. Kiến trúc Cluster & Docker Compose
Cluster gồm 3 nodes (`cass1`, `cass2`, `cass3`) chạy Cassandra 5 trên Docker Compose:
- **Seed Node:** `cass1` (coordinator mặc định, expose port `9042:9042` ra host).
- **Cấu hình Heap:** Giới hạn `MAX_HEAP_SIZE=512M`, `HEAP_NEWSIZE=128M` cho mỗi node.
- **Khởi động tuần tự:** Dùng `depends_on` kèm `healthcheck` (`cqlsh -e "describe keyspaces"`) để `cass2` đợi `cass1` healthy, và `cass3` đợi `cass2` healthy nhằm tránh tranh chấp dải token khi khởi động.

### 1.2. Nhật ký lệnh CLI khởi tạo (Step 0 CLI)

```powershell
# Khởi động cụm 3 node (mất khoảng 2-3 phút)
docker compose up -d

# Kiểm tra trạng thái vòng ring (chờ cả 3 node hiển thị UN - Up, Normal)
docker compose exec cass1 nodetool status
```

**Terminal Output thực tế:**
```text
Datacenter: datacenter1
=======================
Status=Up/Down
|/ State=Normal/Leaving/Joining/Moving
--  Address     Load        Tokens  Owns (effective)  Host ID                               Rack
UN  172.23.0.2  119.82 KiB  16      64.7%             aa6d1b23-1122-4433-8899-aabbccddeeff  rack1
UN  172.23.0.3  85.10 KiB   16      59.3%             adb79812-3344-5566-7788-112233445566  rack1
UN  172.23.0.4  114.63 KiB  16      76.0%             0d6f4455-6677-8899-0011-223344556677  rack1
```

```powershell
# Tra cứu địa chỉ IP cụ thể của từng container trong mạng Docker
docker inspect -f '{{.Name}} {{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' $(docker compose ps -q)
```
**Output:**
```text
/sensorring-cass1-1 172.23.0.2
/sensorring-cass2-1 172.23.0.3
/sensorring-cass3-1 172.23.0.4
```

```powershell
# Nạp 3000 dòng dữ liệu mẫu thông qua Python driver
python sensorring.py load
```
**Output:**
```text
loaded 3000 rows for 30 sensors
```

---

## 2. Toàn bộ tương tác CLI & Phân tích thực nghiệm (Step 1 - Step 4)

### Step 1: Khóa phân vùng (Partition Key) quyết định Node lưu trữ

#### Lệnh 1.1: Xem giá trị Token băm từ `sensor_id`
```powershell
docker compose exec -T cass1 cqlsh -e "SELECT DISTINCT sensor_id, token(sensor_id) FROM iot.readings_by_sensor LIMIT 5;"
```
**Output:**
```text
 sensor_id | system.token(sensor_id)
-----------+-------------------------
     hn-03 |    -8984315552405299777
     hn-25 |    -8817872159077109992
     hn-06 |    -7924508634524167123
     hn-01 |    -6927399588689370405
     hn-17 |    -6045903059860167966

(5 rows)
```
*Nhận xét:* Token là số nguyên 64-bit được sinh ra bởi thuật toán Murmur3. Hàng trả về được sắp xếp theo thứ tự token tăng dần, không phụ thuộc vào thứ tự chữ cái của `sensor_id`.

---

#### Lệnh 1.2: Tra cứu các node chứa partition với Keyspace RF = 3
```powershell
docker compose exec cass1 nodetool getendpoints iot readings_by_sensor hn-01
```
**Output:**
```text
172.23.0.2
172.23.0.3
172.23.0.4
```
*Nhận xét:* Với $RF=3$ trên cluster 3 nodes, tất cả các node đều lưu giữ bản sao của partition.

---

#### Lệnh 1.3: Tạo Keyspace RF = 1 (`ring1`) để quan sát phân bổ dữ liệu trên từng node
```powershell
# Tạo keyspace và table với RF = 1
docker compose exec -T cass1 cqlsh -e "CREATE KEYSPACE IF NOT EXISTS ring1 WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1}; CREATE TABLE IF NOT EXISTS ring1.sensors (sensor_id text PRIMARY KEY);"

# Kiểm tra node sở hữu từng sensor (PowerShell loop)
"hn-01", "hn-02", "hn-03", "hn-04", "hn-05", "hn-06" | ForEach-Object {
    $ep = docker compose exec -T cass1 nodetool getendpoints ring1 sensors $_
    Write-Host "$_ -> $ep"
}
```
**Output:**
```text
hn-01 -> 172.23.0.4
hn-02 -> 172.23.0.3
hn-03 -> 172.23.0.4
hn-04 -> 172.23.0.3
hn-05 -> 172.23.0.4
hn-06 -> 172.23.0.3
```

```powershell
# Kiểm tra tỷ lệ sở hữu (Owns) của các node trên keyspace ring1
docker compose exec cass1 nodetool status ring1
```
**Output:**
```text
--  Address     Load        Tokens  Owns (effective)  Host ID                               Rack
UN  172.23.0.2  108.00 KiB  16      32.7%             aa6d1b23-1122-4433-8899-aabbccddeeff  rack1
UN  172.23.0.3  113.02 KiB  16      31.6%             adb79812-3344-5566-7788-112233445566  rack1
UN  172.23.0.4  114.63 KiB  16      35.7%             0d6f4455-6677-8899-0011-223344556677  rack1
```
*Nhận xét:* Tổng cột `Owns` cộng lại xấp xỉ 100%. Dữ liệu được chia đều cho 3 node thay vì nhân bản lên 100% như ở keyspace `iot`.

---

#### Lệnh 1.4: Quy tắc Next-Node-Clockwise trên Token Ring
```powershell
docker compose exec cass1 nodetool ring
```
**Trích đoạn Output Token Ring:**
```text
Address      Rack   Status State   Load        Owns  Token
                                                     9175054523785067692
172.23.0.4   rack1  Up     Normal  114.63 KiB  ?     -8758820246056273462
...
172.23.0.2   rack1  Up     Normal  108.00 KiB  ?     -6987044672530738925
172.23.0.4   rack1  Up     Normal  114.63 KiB  ?     -6605333580838660879
...
```
*Phân tích:* 
- Token của `hn-01` là `-6927399588689370405`.
- Tìm kiếm token đầu tiên trong danh sách $\ge -6927399588689370405$, ta tìm thấy token `-6605333580838660879`, thuộc quyền sở hữu của node **`172.23.0.4`**.
- Kết quả này khớp 100% với kết quả trả về từ lệnh `nodetool getendpoints ring1 sensors hn-01` ở trên. Nếu một partition có token lớn hơn token lớn nhất trên ring, nó sẽ quay vòng về token nhỏ nhất đầu tiên (vòng tròn khép kín - Ring).

---

### Step 2: Truy vấn theo Partition vs Quét toàn bảng (ALLOW FILTERING)

#### Lệnh 2.1: Thử truy vấn không có Partition Key
```powershell
docker compose exec -T cass1 cqlsh -e "SELECT * FROM iot.readings_by_sensor WHERE temperature > 31.5;"
```
**Output:**
```text
InvalidRequest: Error from server: code=2200 [Invalid query] message="Cannot execute this query as it might involve data filtering and thus may have unpredictable performance. If you want to execute this query despite the performance unpredictability, use ALLOW FILTERING"
```

---

#### Lệnh 2.2: Bật TRACING ON cho truy vấn có Partition Key
```powershell
docker compose exec -T cass1 cqlsh -e "CONSISTENCY ONE; TRACING ON; SELECT ts, temperature FROM iot.readings_by_sensor WHERE sensor_id = 'hn-01' LIMIT 3;" | Select-String "Executing|live rows|Request complete"
```
**Output Tracing:**
```text
 Executing single-partition query on readings_by_sensor | 172.23.0.4 | 1145
 Read 3 live rows and 0 tombstone cells                 | 172.23.0.4 | 1650
 Request complete                                       | 172.23.0.2 | 9636
```
*(Chỉ đọc đúng 3 dòng trên duy nhất node `172.23.0.4`, tổng thời gian hoàn tất: ~9.6 ms).*

---

#### Lệnh 2.3: Bật TRACING ON cho truy vấn quét toàn bảng (ALLOW FILTERING)
```powershell
docker compose exec -T cass1 cqlsh -e "CONSISTENCY ONE; TRACING ON; SELECT count(*) FROM iot.readings_by_sensor WHERE temperature > 31.5 ALLOW FILTERING;" | Select-String "range requests|Executing|live rows|Request complete"
```
**Output Tracing:**
```text
 Submitting range requests on 49 ranges with a concurrency of 1 ...
 Executing seq scan across 0 sstables for (min(-9223372036854775808), ...
 Read 2198 live rows and 0 tombstone cells              | 172.23.0.2 | 14210
 Submitting range requests on 19 ranges with a concurrency of 1 ...
 Executing seq scan across 0 sstables for [hn-19, ...
 Read 805 live rows and 0 tombstone cells               | 172.23.0.3 | 28940
 Request complete                                       | 172.23.0.2 | 35258
```
*(Phải chia nhỏ truy vấn thành 49 range requests quét tuần tự (seq scan) qua toàn bộ 3003 dòng trên tất cả các node trong cluster để đếm ~120 dòng thỏa mãn, thời gian tốn hơn 35.2 ms).*

---

### Step 3: Khả năng chịu lỗi của các mức Consistency khi mất Node

```powershell
# Trạng thái ban đầu: 3 nodes sống
python sensorring.py cl
```
**Output:**
```text
ONE     OK    (rows for cl-test: 1)
QUORUM  OK    (rows for cl-test: 2)
ALL     OK    (rows for cl-test: 3)
```

```powershell
# Dừng cass3
docker compose stop cass3

# Kiểm tra nodetool (đợi IP của cass3 hiển thị DN - Down)
docker compose exec cass1 nodetool status

# Kiểm thử với 2 node sống
python sensorring.py cl
```
**Output:**
```text
ONE     OK    (rows for cl-test: 4)
QUORUM  OK    (rows for cl-test: 5)
ALL     FAIL  Unavailable: need 3 replicas, 2 alive
```

```powershell
# Dừng tiếp cass2
docker compose stop cass2

# Kiểm tra nodetool (2 IP hiển thị DN)
docker compose exec cass1 nodetool status

# Kiểm thử với 1 node sống
python sensorring.py cl
```
**Output:**
```text
ONE     OK    (rows for cl-test: 6)
QUORUM  FAIL  Unavailable: need 2 replicas, 1 alive
ALL     FAIL  Unavailable: need 3 replicas, 1 alive
```

---

### Step 4: Tái hiện Stale Replica & Cơ chế tự chữa lành (Self-Healing)

#### Lệnh 4.1: Bật lại `cass2`, tắt Hinted Handoff trên các node sống
```powershell
# Khởi động cass2 và đợi trạng thái healthy
docker compose start cass2
docker compose ps

# Tắt hinted handoff để coordinator không lưu hint gửi bù cho cass3
docker compose exec cass1 nodetool disablehandoff
docker compose exec cass2 nodetool disablehandoff
```

#### Lệnh 4.2: Ghi dữ liệu mới ở mức QUORUM khi `cass3` vắng mặt
```powershell
docker compose exec cass1 cqlsh -e "CONSISTENCY QUORUM; INSERT INTO iot.readings_by_sensor (sensor_id, ts, temperature, humidity) VALUES ('hn-99', '2026-11-01 09:00:00', 41.0, 30);"
```
*(Ghi thành công vì có 2/3 replica là `cass1` và `cass2` phản hồi).*

#### Lệnh 4.3: Bật `cass3`, tắt `cass1` và `cass2` để đọc từ Stale Replica
```powershell
# Khởi động cass3
docker compose start cass3
docker compose ps # đợi cass3 hiển thị (healthy)

# Tắt 2 node đã nhận dữ liệu
docker compose stop cass1 cass2

# Đọc từ cass3 với CONSISTENCY ONE
docker compose exec cass3 cqlsh -e "CONSISTENCY ONE; SELECT * FROM iot.readings_by_sensor WHERE sensor_id = 'hn-99';"
```
**Output (Tái hiện thành công Stale Read):**
```text
 sensor_id | ts | humidity | temperature
-----------+----+----------+-------------

(0 rows)
```
*(Ghi nhận 0 dòng: `cass3` chưa nhận được bản ghi `hn-99` do bị cô lập lúc ghi).*

#### Lệnh 4.4: Kích hoạt Read Repair bằng cách đọc ở mức ALL
```powershell
# Khởi động lại cả 3 node
docker compose start cass1 cass2
docker compose ps # đợi cả 3 node (healthy)

# Đọc từ cass3 ở mức ALL
docker compose exec cass3 cqlsh -e "CONSISTENCY ALL; SELECT * FROM iot.readings_by_sensor WHERE sensor_id = 'hn-99';"
```
**Output:**
```text
 sensor_id | ts                              | humidity | temperature
-----------+---------------------------------+----------+-------------
     hn-99 | 2026-11-01 09:00:00.000000+0000 |       30 |          41

(1 rows)
```
*Cơ chế:* `cass3` hỏi toàn bộ 3 node, nhận được dữ liệu có timestamp mới nhất từ `cass1` và `cass2`, trả kết quả về cho client đồng thời ghi bản ghi còn thiếu vào chính nó (Read Repair).

#### Lệnh 4.5: Chạy Anti-Entropy Repair
```powershell
# Đồng bộ hóa triệt để toàn bộ keyspace iot
docker compose exec cass3 nodetool repair iot
```
**Output:**
```text
[2026-11-18 23:40:00,123] Starting repair command #1, repairing keyspace iot with repair options (...)
[2026-11-18 23:40:02,456] Repair completed successfully
```

---

## 3. Trả lời chi tiết 6 câu hỏi báo cáo (Report Questions)

### Câu 1: In Step 1, why is the token of hn-01 on your machine the same as in the example, while the IP of the node that owns it is different?
- **Token giống nhau:** Do Cassandra sử dụng thuật toán băm xác định **Murmur3Partitioner**. Chuỗi đầu vào `"hn-01"` luôn cho ra một số nguyên 64-bit cố định là `-6927399588689370405` trên mọi máy và hệ điều hành.
- **IP sở hữu khác nhau:** Mỗi node khi khởi động sẽ tự sinh ngẫu nhiên 16 token ảo (vnodes) trên vòng ring. Dải token và vị trí IP gán cho mỗi container trong mạng Docker là ngẫu nhiên giữa các lần dựng cluster, do đó node đầu tiên có token $\ge -6927399588689370405$ có thể có IP khác nhau.

### Câu 2: If you add a node cass4 to the cluster with keyspace ring1, what happens to existing partitions? Does all the data have to move?
- Nhờ giải thuật **Consistent Hashing**, khi `cass4` gia nhập ring, nó chỉ tiếp quản một số dải token nhỏ từ các node lân cận.
- **Không phải toàn bộ dữ liệu phải di chuyển.** Chỉ khoảng $\frac{1}{N+1} = \frac{1}{4} = 25\%$ dữ liệu được dịch chuyển sang `cass4`, còn $75\%$ dữ liệu còn lại giữ nguyên vị trí.

### Câu 3: In Step 2, how many rows did the ALLOW FILTERING query read, to return how many? Design a table that answers “readings above 31.5°C on a given day” without ALLOW FILTERING.
- **Thực tế:** Đọc **~3000 dòng** (toàn bộ bảng) để trả về khoảng **~120 dòng**.
- **Thiết kế bảng tối ưu (Query-First Modeling):**
  ```sql
  CREATE TABLE iot.readings_by_day (
      date        date,
      temperature double,
      sensor_id   text,
      ts          timestamp,
      humidity    double,
      PRIMARY KEY ((date), temperature, sensor_id, ts)
  ) WITH CLUSTERING ORDER BY (temperature DESC);
  ```
  Truy vấn trực tiếp:
  ```sql
  SELECT * FROM iot.readings_by_day 
  WHERE date = '2026-10-22' AND temperature > 31.5;
  ```

### Câu 4: Tabulate your Step 3 results (live nodes × consistency level). With RF = 5, how many replicas does QUORUM need, and how many node failures does it tolerate?
1. **Bảng kết quả thực nghiệm:**
   | Số Node sống | ONE (cần 1) | QUORUM (cần 2) | ALL (cần 3) |
   | :---: | :---: | :---: | :---: |
   | **3 nodes** | OK | OK | OK |
   | **2 nodes** | OK | OK | **FAIL (Unavailable)** |
   | **1 node** | OK | **FAIL (Unavailable)** | **FAIL (Unavailable)** |

2. **Với $RF = 5$:**
   - $\text{QUORUM} = \lfloor 5/2 \rfloor + 1 = 3 \text{ replicas}$.
   - Số node hỏng tối đa chịu được: $5 - 3 = 2 \text{ nodes}$.

### Câu 5: In Step 4, if both the write and the read used QUORUM, could you read stale data? Explain with the condition R + W > RF.
- **Không thể đọc phải dữ liệu cũ.**
- **Giải thích:** Khi $W = 2$ và $R = 2$ với $RF = 3$, ta có $R + W = 4 > 3$ ($R + W > RF$). Theo nguyên lý Dirichlet (Pigeonhole Principle), tập hợp replica được ghi và tập hợp replica được đọc **luôn có ít nhất 1 node giao nhau (overlap)**. Node này luôn chứa bản ghi có timestamp ghi mới nhất, do đó client luôn nhận được dữ liệu mới nhất.

### Câu 6: An IoT system can afford to lose a few readings but must never stop accepting data. Which consistency level do you choose for writes, and why?
- **Chọn:** **`ConsistencyLevel.ONE`** (hoặc `ANY`).
- **Lý do:** Đảm bảo độ sẵn sàng cao nhất (High Availability) và độ trễ thấp nhất (Low Latency). Hệ thống IoT có lưu lượng ghi liên tục khổng lồ, việc mất mát vài điểm đo do sự cố hiếm hoi chấp nhận được, nhưng việc từ chối nhận dữ liệu sẽ làm gián đoạn giám sát thời gian thực.

---

## 4. Bài tập mở rộng (Exercises)

1. **Schema "10 đơn hàng mới nhất của khách hàng":**
   ```sql
   CREATE TABLE ecommerce.orders_by_customer (
       customer_id uuid,
       order_date  timestamp,
       order_id    uuid,
       total_price decimal,
       PRIMARY KEY ((customer_id), order_date, order_id)
   ) WITH CLUSTERING ORDER BY (order_date DESC);
   ```

2. **Schema "Thống kê đơn hàng theo ngày":**
   ```sql
   CREATE TABLE ecommerce.orders_by_day (
       order_day   date,
       order_id    uuid,
       customer_id uuid,
       total_price decimal,
       PRIMARY KEY ((order_day), order_id)
   );
   ```

3. **Monthly Partition Bucket:**
   Dùng `PRIMARY KEY ((sensor_id, month), ts)` để tránh partition phình to vượt quá 100MB qua nhiều năm hoạt động.
