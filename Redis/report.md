# Lab Report: High-Concurrency Ticket Booking System with Redis (TicketRush)

## 1. Overview & Architecture

Hệ thống bán vé trực tuyến **TicketRush** giải quyết bài toán tải cao (high concurrency) và điều kiện tranh định (race conditions) khi hàng nghìn người dùng cùng truy cập và tranh mua vé đồng thời. Redis đóng vai trò là tầng lưu trữ trên bộ nhớ đệm (in-memory fast layer) tốc độ cao, hỗ trợ các thao tác nguyên tử (atomic operations) và scripting qua Lua.

### Key Layout

| Key Pattern | Redis Type | Ý nghĩa & Mục đích |
| :--- | :--- | :--- |
| `hold:{event}:{seat}` | String (with TTL) | Ai đang giữ ghế trong thời gian giữ tạm thời (ví dụ: 300s để thanh toán). |
| `sold:{event}` | Set | Tập hợp các ghế đã được xác nhận thanh toán thành công. |
| `stock:{event}` | String (Counter) | Số lượng vé mở (general admission) còn lại trong kho. |
| `buyers:{event}` | Set | Tập hợp những người dùng đã mua vé mở thành công. |
| `rl:sliding:{user}` | Sorted Set | Lưu timestamp các request gần nhất của user phục vụ Rate Limiting. |
| `queue:{event}` | Sorted Set | Phòng chờ ảo (virtual waiting room), `score` là timestamp lúc gia nhập. |

---

## 2. Các bước triển khai & Kết quả thực nghiệm

### Step 1: Giữ chỗ & Khắc phục Race Condition (Seat Holds)
- **Vấn đề (Naive Check-then-act):** Cặp lệnh `GET` sau đó kiểm tra và gọi `SET` tạo ra một khoảng trễ (window of vulnerability). Khi 50 luồng đồng thời chọn ghế `A1`, nhiều luồng đều thấy ghế trống (`None`) dẫn đến nhiều người cùng giữ một ghế (`winners > 1`).
- **Giải pháp (SET NX EX):** Sử dụng tham số `nx=True` (chỉ đặt nếu chưa tồn tại) và `ex=300` (thời gian sống 300s) trong một lệnh đơn nguyên tử duy nhất trên Redis engine.
- **Kết quả thực nghiệm:**
  - `hold_naive`: **6 winners** (xảy ra lỗi bán trùng/overselling).
  - `hold_nx`: **1 winner duy nhất** (100% an toàn trước race condition).

### Step 2: Vòng đời vé nguyên tử với Lua Scripts (Hold, Confirm, Release)
- **Vấn đề:** 
  1. Ghế đã bán (`sold`) không được phép cho giữ lại.
  2. Chỉ người giữ ghế mới có quyền xác nhận (`confirm`) hoặc giải phóng (`release`).
- **Giải pháp:** Sử dụng 3 Lua scripts chạy nguyên tử trên Redis server:
  - `HOLD`: Kiểm tra `SISMEMBER sold:{event}` trước, nếu chưa bán mới thực hiện `SET ... NX EX`.
  - `CONFIRM`: Kiểm tra `GET == user`, sau đó `SADD sold:{event}` và `DEL hold:{event}:{seat}`.
  - `RELEASE`: Kiểm tra `GET == user`, xóa key giữ ghế nếu đúng chủ sở hữu.
- **Kết quả thực nghiệm:**
  - `hold("c1", "A1", "linh")` -> `True`
  - `hold("c1", "A1", "nam")` -> `False` (bị từ chối vì Linh đang giữ)
  - `confirm("c1", "A1", "nam")` -> `False` (bị từ chối vì Nam không phải chủ giữ)
  - `confirm("c1", "A1", "linh")` -> `True` (xác nhận bán thành công)
  - `hold("c1", "A1", "nam")` -> `False` (bị từ chối vì ghế đã bán)
  - `seat_map("c1", ["A1", "A2"])` -> `{'A1': 'sold', 'A2': 'free'}`

### Step 3: Flash Sale vé phổ thông (General Admission Counter)
- **Cơ chế:** Dùng lệnh `DECR` nguyên tử để giảm số lượng vé trong kho `stock:{event}`. Nếu kết quả trả về `< 0`, lập tức gọi `INCR` để hoàn lại và trả về `False`.
- **Kết quả thực nghiệm:** 500 luồng đồng thời tranh mua 100 vé:
  - Số vé bán ra: **100**
  - Số lượng vé còn lại: **0**
  - Số người mua thành công: **100**
  - Không hề xảy ra tình trạng bán khống (overselling).

### Step 4: Giới hạn tần suất yêu cầu (Rate Limiting)
- **Fixed Window:** Dùng counter theo block thời gian với `pipeline.incr` và `expire(nx=True)`. Hạn chế: có thể bị burst gấp đôi giới hạn tại ranh giới chuyển giao giữa 2 window.
- **Sliding Window:** Dùng Sorted Set (`ZREMRANGEBYSCORE`, `ZCARD`, `ZADD`, `PEXPIRE` trong Lua script) để theo dõi chính xác các mốc thời gian của request trong 10 giây gần nhất.
- **Kết quả thực nghiệm:**
  - User `bot` gửi liên tiếp 6 request: 5 request đầu được phép (`True`), request thứ 6 bị chặn (`False`), trả về trạng thái `"rate_limited"`.

### Step 5: Phòng chờ ảo (Virtual Waiting Room)
- Sử dụng Sorted Set với `score` là `join_time` (`time.time()`).
- Sử dụng cờ `NX` trong `ZADD` để đảm bảo nếu người dùng vô tình reload/gọi lại thì vị trí trong hàng đợi không bị thay đổi.
- Sử dụng `ZPOPMIN` để mở cửa cho từng lô $n$ người đầu tiên vào hệ thống.
- **Kết quả thực nghiệm:** Thứ tự xếp hàng `u1, u2, u3` được bảo toàn chính xác theo thứ tự gia nhập.

### Step 6: Kiểm thử tải tổng hợp (Stress Test)
- **Kịch bản:** 100 người dùng đồng thời tranh chấp 20 ghế (từ `S1` đến `S20`). Mỗi người duyệt danh sách ghế, giữ ghế trống đầu tiên, giả lập thời gian thanh toán và xác nhận mua.
- **Kết quả:** Chạy lặp lại 10 lần liên tiếp:
  - Cả 10 lần đều đạt chuẩn: **20 vé bán ra, 20 ghế phân biệt, 20 phần tử trong Set `sold:c1`** (kết quả `20 20 20`). Không có bất kỳ vé nào bị bán trùng hay bán sót.

---

## 3. Trả lời câu hỏi báo cáo (Report Questions)

### Câu 1: In Step 1, why does SET NX EX avoid the race while GET followed by SET does not?
**Trả lời:**
- Thao tác `GET` theo sau bởi `SET` là kiểu mẫu thao tác phi nguyên tử (*check-then-act*). Giữa thời điểm lệnh `GET` trả về kết quả (thấy key chưa tồn tại) và lệnh `SET` được gửi lên, các tiến trình/luồng khác cũng thực hiện `GET` và nhận cùng kết quả là rỗng. Do đó, tất cả các luồng này đều tin rằng ghế còn trống và lần lượt ghi đè (`SET`), dẫn đến nhiều người cùng giữ một ghế.
- Lệnh `SET ... NX EX` là **một lệnh nguyên tử duy nhất (single atomic command)** được thực thi trọn vẹn trong một lượt xử lý đơn luồng của Redis. Việc kiểm tra khóa tồn tại (`NX`) và gán giá trị kèm thời gian hết hạn (`EX`) diễn ra tức thì, không một lệnh nào khác có thể chen vào giữa, đảm bảo chỉ duy nhất một client thực hiện thành công.

---

### Câu 2: What happens to a seat if a customer holds it and closes the browser? Which Redis feature handles that?
**Trả lời:**
- Khi khách hàng giữ ghế rồi tắt trình duyệt hoặc không thanh toán, ghế đó sẽ **tự động trở về trạng thái tự do (`free`)** sau khoảng thời gian quy định (ví dụ 300 giây) mà hệ thống không cần chạy bất kỳ cronjob hay background worker nào để quét dọn.
- Tính năng xử lý việc này là **TTL (Time-To-Live / Key Expiration)** của Redis (được thiết lập thông qua cờ `EX` trong lệnh `SET` hoặc lệnh `EXPIRE`). Khi hết thời gian TTL, Redis tự động giải phóng key `hold:{event}:{seat}`.

---

### Câu 3: What would go wrong in Step 2 if confirm did not compare the hold’s owner?
**Trả lời:**
Nếu `confirm` không so sánh người sở hữu (`GET KEYS[1] == ARGV[1]`):
1. **Chiếm đoạt vé:** Người dùng B có thể đoán mã ghế và gọi `confirm` để cướp và thanh toán cho ghế mà người dùng A đang giữ hợp lệ.
2. **Lỗi sau khi hết hạn (Expired Hold Race):** Nếu người dùng A giữ chỗ nhưng để hết hạn TTL, ngay sau đó người dùng B vào giữ ghế này. Nếu không kiểm tra chủ sở hữu, người dùng A (vẫn đang mở trang thanh toán trễ) bấm xác nhận thì hệ thống sẽ ghi nhận ghế cho người dùng A và xóa mất lượt giữ hợp lệ của người dùng B.

---

### Câu 4: When is fixed-window rate limiting good enough, and when do you need a sliding window?
**Trả lời:**
- **Fixed-window** đủ tốt khi:
  - Hệ thống chấp nhận được hiện tượng đột biến lưu lượng (traffic burst) lên tới gấp đôi mức giới hạn tại thời điểm giao thoa giữa 2 cửa sổ thời gian (ví dụ: gửi 5 request ở giây 9.9 và 5 request ở giây 10.1).
  - Cần tối ưu tài nguyên tối đa (bộ nhớ cực thấp, chỉ cần 1 counter số nguyên đơn giản). Thường áp dụng cho các API public thông thường, scraping nhẹ.
- **Sliding-window** cần thiết khi:
  - Các sự kiện nhạy cảm cao như Flash Sale, phòng vé máy bay/sự kiện, ngăn chặn bot đặt vé hoặc các API giới hạn tài chính/SMS OTP.
  - Cần đảm bảo tuyệt đối không bao giờ vượt quá số lượng request quy định trong bất kỳ khung thời gian trượt liên tục nào.

---

### Câu 5: Redis is your fast layer here. Which data would you also write to a durable database, and when?
**Trả lời:**
- **Dữ liệu cần lưu vào Database bền vững (PostgreSQL / MySQL):**
  - Thông tin đơn hàng và vé đã thanh toán: `order_id`, `event_id`, `seat_id`, `user_id`, `price`, `payment_transaction_id`, `status='PAID'`, `created_at`.
  - Lịch sử giao dịch thanh toán và thông tin khách hàng.
- **Thời điểm ghi:**
  - Ghi vào CSDL bền vững **ngay sau khi hoàn tất xác nhận thanh toán thành công** (sau khi cổng thanh toán trả về IPN thành công và lệnh `confirm` trên Redis được thực hiện).
  - Có thể sử dụng cơ chế *Transactional Outbox Pattern* hoặc Message Queue (Kafka/RabbitMQ) để đồng bộ đảm bảo tính toàn vẹn (eventual consistency). Nếu Redis gặp sự cố hoặc restart, toàn bộ dữ liệu vé đã bán vẫn được khôi phục nguyên vẹn từ CSDL quan hệ chính.
