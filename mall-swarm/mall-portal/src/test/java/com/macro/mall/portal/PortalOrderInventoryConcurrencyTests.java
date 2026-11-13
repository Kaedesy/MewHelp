package com.macro.mall.portal;

import com.macro.mall.portal.dao.PortalOrderDao;
import com.macro.mall.portal.dao.OutboxEventDao;
import com.macro.mall.portal.domain.OmsOrderIdempotency;
import com.macro.mall.model.OmsOrder;
import com.macro.mall.model.OmsOrderSetting;
import com.macro.mall.mapper.OmsOrderSettingMapper;
import com.macro.mall.model.UmsMember;
import com.macro.mall.model.UmsMemberReceiveAddress;
import com.macro.mall.portal.component.CancelOrderSender;
import com.macro.mall.portal.component.OutboxEventPublisher;
import com.macro.mall.portal.domain.CartPromotionItem;
import com.macro.mall.portal.domain.OrderParam;
import com.macro.mall.portal.service.OmsCartItemService;
import com.macro.mall.portal.service.OmsPortalOrderService;
import com.macro.mall.portal.service.UmsMemberReceiveAddressService;
import com.macro.mall.portal.service.UmsMemberService;
import com.macro.mall.common.service.RedisService;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.RepeatedTest;
import org.junit.jupiter.api.RepetitionInfo;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import org.springframework.http.MediaType;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.PreparedStatementCreator;
import org.springframework.jdbc.support.GeneratedKeyHolder;
import org.springframework.jdbc.support.KeyHolder;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.test.context.bean.override.mockito.MockitoBean;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.atomic.AtomicInteger;
import java.math.BigDecimal;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.Mockito.when;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.doThrow;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.Assertions.assertNull;

/**
 * MySQL integration tests for stock concurrency and order idempotency.
 */
@SpringBootTest(properties = {
        "spring.rabbitmq.listener.simple.auto-startup=false",
        "mall.outbox.enabled=false"
})
@AutoConfigureMockMvc
class PortalOrderInventoryConcurrencyTests {
    @Autowired
    private PortalOrderDao portalOrderDao;
    @Autowired
    private OutboxEventDao outboxEventDao;
    @Autowired
    private JdbcTemplate jdbcTemplate;
    @Autowired
    private PlatformTransactionManager transactionManager;
    @Autowired
    private OmsPortalOrderService portalOrderService;
    @Autowired
    private MockMvc mockMvc;
    @Autowired
    private ObjectMapper objectMapper;
    @MockitoBean
    private UmsMemberService memberService;
    @MockitoBean
    private OmsCartItemService cartItemService;
    @MockitoBean
    private UmsMemberReceiveAddressService addressService;
    @MockitoBean
    private RedisService redisService;
    @MockitoBean
    private OmsOrderSettingMapper orderSettingMapper;
    @MockitoBean
    private CancelOrderSender cancelOrderSender;

    @Test
    void concurrentReservationsMustNotExceedAvailableStock() throws Exception {
        int initialStock = 40;
        int requestCount = 100;
        int workerCount = 32;
        long skuId = createSku(initialStock);
        ExecutorService executor = Executors.newFixedThreadPool(workerCount);
        CountDownLatch ready = new CountDownLatch(workerCount);
        CountDownLatch start = new CountDownLatch(1);
        AtomicInteger successfulReservations = new AtomicInteger();
        List<Future<?>> futures = new ArrayList<>();

        try {
            for (int i = 0; i < requestCount; i++) {
                futures.add(executor.submit(() -> {
                    ready.countDown();
                    if (!start.await(15, TimeUnit.SECONDS)) {
                        throw new IllegalStateException("Test operation failed");
                    }
                    if (portalOrderDao.lockSkuStock(skuId, 1) == 1) {
                        successfulReservations.incrementAndGet();
                    }
                    return null;
                }));
            }

            assertTrue(ready.await(15, TimeUnit.SECONDS), "Concurrent test workers did not become ready");
            start.countDown();
            for (Future<?> future : futures) {
                future.get(30, TimeUnit.SECONDS);
            }

            assertEquals(initialStock, successfulReservations.get());
            assertEquals(initialStock, readLockStock(skuId));
            assertEquals(0, readAvailableStock(skuId));
        } finally {
            start.countDown();
            executor.shutdownNow();
            executor.awaitTermination(10, TimeUnit.SECONDS);
            deleteSku(skuId);
        }
    }

    @RepeatedTest(3)
    void inventoryReservationStressMustReportThroughputWithoutOverselling(RepetitionInfo repetitionInfo) throws Exception {
        int initialStock = 200;
        int requestCount = 500;
        int workerCount = 50;
        long skuId = createSku(initialStock);
        ExecutorService executor = Executors.newFixedThreadPool(workerCount);
        CountDownLatch ready = new CountDownLatch(workerCount);
        CountDownLatch start = new CountDownLatch(1);
        AtomicInteger successfulReservations = new AtomicInteger();
        List<Future<?>> futures = new ArrayList<>();

        try {
            for (int i = 0; i < requestCount; i++) {
                futures.add(executor.submit(() -> {
                    ready.countDown();
                    if (!start.await(15, TimeUnit.SECONDS)) {
                        throw new IllegalStateException("Stress-test workers did not start in time");
                    }
                    if (portalOrderDao.lockSkuStock(skuId, 1) == 1) {
                        successfulReservations.incrementAndGet();
                    }
                    return null;
                }));
            }

            assertTrue(ready.await(15, TimeUnit.SECONDS), "Stress-test workers did not become ready");
            long startedAt = System.nanoTime();
            start.countDown();
            for (Future<?> future : futures) {
                future.get(45, TimeUnit.SECONDS);
            }
            long elapsedMillis = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - startedAt);

            int accepted = successfulReservations.get();
            int rejected = requestCount - accepted;
            int oversell = Math.max(accepted - initialStock, 0);
            double throughput = requestCount * 1000.0 / Math.max(elapsedMillis, 1);
            System.out.printf(Locale.ROOT,
                    "Inventory stress result: repetition=%d, requests=%d, workers=%d, stock=%d, accepted=%d, rejected=%d, oversell=%d, elapsed_ms=%d, throughput_req_s=%.2f%n",
                    repetitionInfo.getCurrentRepetition(), requestCount, workerCount, initialStock,
                    accepted, rejected, oversell, elapsedMillis, throughput);

            assertEquals(initialStock, accepted);
            assertEquals(requestCount - initialStock, rejected);
            assertEquals(0, oversell);
            assertEquals(initialStock, readLockStock(skuId));
            assertEquals(0, readAvailableStock(skuId));
        } finally {
            start.countDown();
            executor.shutdownNow();
            executor.awaitTermination(10, TimeUnit.SECONDS);
            deleteSku(skuId);
        }
    }

    @Test
    void invalidOrInsufficientQuantityMustNotChangeStock() {
        long skuId = createSku(3);
        try {
            assertEquals(0, portalOrderDao.lockSkuStock(skuId, 4));
            assertEquals(0, portalOrderDao.lockSkuStock(skuId, 0));
            assertEquals(0, portalOrderDao.lockSkuStock(skuId, -1));
            assertEquals(0, readLockStock(skuId));
        } finally {
            deleteSku(skuId);
        }
    }

    @Test
    void negativeLegacyLockStockMustNotCreatePhantomAvailability() {
        long skuId = createSku(5, -10);
        try {
            assertEquals(1, portalOrderDao.lockSkuStock(skuId, 5));
            assertEquals(5, readLockStock(skuId));
            assertEquals(0, readAvailableStock(skuId));
            assertEquals(0, portalOrderDao.lockSkuStock(skuId, 1));
        } finally {
            deleteSku(skuId);
        }
    }

    @Test
    void failedSkuReservationMustRollbackEarlierSkuReservations() {
        long firstSkuId = createSku(10);
        long secondSkuId = createSku(1);
        TransactionTemplate transactionTemplate = new TransactionTemplate(transactionManager);

        try {
            assertThrows(IllegalStateException.class, () -> transactionTemplate.execute(status -> {
                if (portalOrderDao.lockSkuStock(firstSkuId, 5) != 1) {
                    throw new IllegalStateException("Test operation failed");
                }
                if (portalOrderDao.lockSkuStock(secondSkuId, 2) != 1) {
                    throw new IllegalStateException("Test operation failed");
                }
                return null;
            }));

            assertEquals(0, readLockStock(firstSkuId));
            assertEquals(0, readLockStock(secondSkuId));
        } finally {
            deleteSku(firstSkuId);
            deleteSku(secondSkuId);
        }
    }

    @Test
    void concurrentRequestsWithTheSameIdempotencyKeyMustHaveOneOwner() throws Exception {
        Long memberId = ThreadLocalRandom.current().nextLong(1, Long.MAX_VALUE);
        String idempotencyKey = UUID.randomUUID().toString();
        String requestHash = "a".repeat(64);
        int requestCount = 80;
        int workerCount = 16;
        ExecutorService executor = Executors.newFixedThreadPool(workerCount);
        CountDownLatch ready = new CountDownLatch(workerCount);
        CountDownLatch start = new CountDownLatch(1);
        AtomicInteger claimedCount = new AtomicInteger();
        AtomicInteger duplicateCount = new AtomicInteger();
        List<Future<?>> futures = new ArrayList<>();

        try {
            for (int i = 0; i < requestCount; i++) {
                futures.add(executor.submit(() -> {
                    ready.countDown();
                    if (!start.await(15, TimeUnit.SECONDS)) {
                        throw new IllegalStateException("Test operation failed");
                    }
                    int inserted = portalOrderDao.insertIdempotencyRequest(memberId, idempotencyKey, requestHash);
                    if (inserted == 1) {
                        claimedCount.incrementAndGet();
                    } else {
                        duplicateCount.incrementAndGet();
                    }
                    return null;
                }));
            }

            assertTrue(ready.await(15, TimeUnit.SECONDS), "Concurrent idempotency workers did not become ready");
            start.countDown();
            for (Future<?> future : futures) {
                future.get(30, TimeUnit.SECONDS);
            }

            assertEquals(1, claimedCount.get());
            assertEquals(requestCount - 1, duplicateCount.get());
            assertEquals(0, portalOrderDao.insertIdempotencyRequest(
                    memberId, idempotencyKey, "c".repeat(64)));
            OmsOrderIdempotency storedRequest = portalOrderDao
                    .selectIdempotencyRequestForUpdate(memberId, idempotencyKey);
            assertEquals(requestHash, storedRequest.getRequestHash());
            assertEquals("PROCESSING", storedRequest.getStatus());
        } finally {
            start.countDown();
            executor.shutdownNow();
            executor.awaitTermination(10, TimeUnit.SECONDS);
            jdbcTemplate.update("DELETE FROM oms_order_idempotency WHERE member_id = ? AND idempotency_key = ?",
                    memberId, idempotencyKey);
        }
    }

    @Test
    void idempotencyClaimAndStockReservationMustRollbackTogether() {
        Long memberId = ThreadLocalRandom.current().nextLong(1, Long.MAX_VALUE);
        String idempotencyKey = UUID.randomUUID().toString();
        String requestHash = "b".repeat(64);
        long skuId = createSku(5);
        TransactionTemplate transactionTemplate = new TransactionTemplate(transactionManager);

        try {
            assertThrows(IllegalStateException.class, () -> transactionTemplate.execute(status -> {
                assertEquals(1, portalOrderDao.insertIdempotencyRequest(memberId, idempotencyKey, requestHash));
                assertEquals(1, portalOrderDao.lockSkuStock(skuId, 2));
                throw new IllegalStateException("Test operation failed");
            }));

            assertEquals(0, readLockStock(skuId));
            assertNull(portalOrderDao.selectIdempotencyRequestForUpdate(memberId, idempotencyKey));
        } finally {
            jdbcTemplate.update("DELETE FROM oms_order_idempotency WHERE member_id = ? AND idempotency_key = ?",
                    memberId, idempotencyKey);
            deleteSku(skuId);
        }
    }

    @Test
    void concurrentPaymentAndCancellationMustApplyInventorySideEffectOnlyOnce() throws Exception {
        Long memberId = ThreadLocalRandom.current().nextLong(1, Long.MAX_VALUE);
        long skuId = createSku(10, 1);
        String orderSn = "transition-" + UUID.randomUUID();
        long orderId = createPendingOrder(memberId, orderSn, skuId);
        ExecutorService executor = Executors.newFixedThreadPool(2);
        CountDownLatch ready = new CountDownLatch(2);
        CountDownLatch start = new CountDownLatch(1);

        try {
            Future<Integer> payment = executor.submit(() -> {
                ready.countDown();
                if (!start.await(15, TimeUnit.SECONDS)) {
                    throw new IllegalStateException("Payment worker did not start");
                }
                return portalOrderService.paySuccess(orderId, 1);
            });
            Future<?> cancellation = executor.submit(() -> {
                ready.countDown();
                if (!start.await(15, TimeUnit.SECONDS)) {
                    throw new IllegalStateException("Cancellation worker did not start");
                }
                portalOrderService.cancelOrder(orderId);
                return null;
            });

            assertTrue(ready.await(15, TimeUnit.SECONDS), "Payment and cancellation workers did not become ready");
            start.countDown();
            int paymentResult = payment.get(30, TimeUnit.SECONDS);
            cancellation.get(30, TimeUnit.SECONDS);

            Integer status = jdbcTemplate.queryForObject(
                    "SELECT status FROM oms_order WHERE id = ?", Integer.class, orderId);
            assertTrue(status == 1 || status == 4);
            assertEquals(0, readLockStock(skuId));
            if (status == 1) {
                assertEquals(9, readAvailableStock(skuId));
                assertEquals(1, paymentResult);
                assertEquals(Boolean.TRUE, jdbcTemplate.queryForObject(
                        "SELECT payment_time IS NOT NULL FROM oms_order WHERE id = ?", Boolean.class, orderId));
            } else {
                assertEquals(10, readAvailableStock(skuId));
                assertEquals(0, paymentResult);
                assertEquals(Boolean.FALSE, jdbcTemplate.queryForObject(
                        "SELECT payment_time IS NOT NULL FROM oms_order WHERE id = ?", Boolean.class, orderId));
            }

            assertEquals(0, portalOrderService.paySuccess(orderId, 1));
            portalOrderService.cancelOrder(orderId);
            assertEquals(0, readLockStock(skuId));
            assertEquals(status == 1 ? 9 : 10, readAvailableStock(skuId));
        } finally {
            start.countDown();
            executor.shutdownNow();
            executor.awaitTermination(10, TimeUnit.SECONDS);
            jdbcTemplate.update("DELETE FROM oms_order_item WHERE order_id = ?", orderId);
            jdbcTemplate.update("DELETE FROM oms_order WHERE id = ?", orderId);
            deleteSku(skuId);
        }
    }

    @Test
    void failedInventoryConfirmationMustRollbackPaymentStatusTransition() {
        Long memberId = ThreadLocalRandom.current().nextLong(1, Long.MAX_VALUE);
        long skuId = createSku(10);
        String orderSn = "rollback-" + UUID.randomUUID();
        long orderId = createPendingOrder(memberId, orderSn, skuId);

        try {
            assertThrows(RuntimeException.class, () -> portalOrderService.paySuccess(orderId, 1));
            assertEquals(0, jdbcTemplate.queryForObject(
                    "SELECT status FROM oms_order WHERE id = ?", Integer.class, orderId));
            assertEquals(0, readLockStock(skuId));
            assertEquals(10, readAvailableStock(skuId));
        } finally {
            jdbcTemplate.update("DELETE FROM oms_order_item WHERE order_id = ?", orderId);
            jdbcTemplate.update("DELETE FROM oms_order WHERE id = ?", orderId);
            deleteSku(skuId);
        }
    }

    @Test
    void timeoutCancellationMustReleaseInventoryOnlyForOrdersItTransitions() {
        Long memberId = ThreadLocalRandom.current().nextLong(1, Long.MAX_VALUE);
        List<Long> existingPendingOrderIds = jdbcTemplate.queryForList(
                "SELECT id FROM oms_order WHERE status = 0", Long.class);
        jdbcTemplate.update("UPDATE oms_order SET status = 4 WHERE status = 0");
        long skuId = createSku(10, 1);
        String orderSn = "timeout-" + UUID.randomUUID();
        long orderId = createPendingOrder(memberId, orderSn, skuId);
        OmsOrderSetting setting = new OmsOrderSetting();
        setting.setNormalOrderOvertime(0);
        when(orderSettingMapper.selectByPrimaryKey(1L)).thenReturn(setting);

        try {
            assertEquals(1, portalOrderService.cancelTimeOutOrder());
            assertEquals(4, jdbcTemplate.queryForObject(
                    "SELECT status FROM oms_order WHERE id = ?", Integer.class, orderId));
            assertEquals(0, readLockStock(skuId));
            assertEquals(10, readAvailableStock(skuId));
            assertEquals(0, portalOrderService.cancelTimeOutOrder());
            assertEquals(0, readLockStock(skuId));
        } finally {
            jdbcTemplate.update("DELETE FROM oms_order_item WHERE order_id = ?", orderId);
            jdbcTemplate.update("DELETE FROM oms_order WHERE id = ?", orderId);
            deleteSku(skuId);
            for (Long existingOrderId : existingPendingOrderIds) {
                jdbcTemplate.update("UPDATE oms_order SET status = 0 WHERE id = ?", existingOrderId);
            }
        }
    }

    @Test
    void successfulOrderRetryMustReplayWithoutReservingStockAgain() throws Exception {
        Long memberId = ThreadLocalRandom.current().nextLong(1, Long.MAX_VALUE);
        String idempotencyKey = UUID.randomUUID().toString();
        long skuId = createSku(5);
        OmsOrder createdOrder = null;

        UmsMember member = new UmsMember();
        member.setId(memberId);
        member.setUsername("idempotency-test-" + memberId);
        member.setIntegration(0);
        when(memberService.getCurrentMember()).thenReturn(member);

        CartPromotionItem cartItem = new CartPromotionItem();
        cartItem.setId(1L);
        cartItem.setProductId(1L);
        cartItem.setProductSkuId(skuId);
        cartItem.setProductName("idempotency-test-product");
        cartItem.setPrice(new BigDecimal("12.50"));
        cartItem.setQuantity(2);
        cartItem.setProductSkuCode("test-sku-" + skuId);
        cartItem.setReduceAmount(BigDecimal.ZERO);
        cartItem.setIntegration(0);
        cartItem.setGrowth(0);
        when(cartItemService.listPromotion(memberId, List.of(1L))).thenReturn(List.of(cartItem));

        UmsMemberReceiveAddress address = new UmsMemberReceiveAddress();
        address.setId(1L);
        address.setName("test-recipient");
        address.setPhoneNumber("13800000000");
        address.setPostCode("100000");
        address.setProvince("test-province");
        address.setCity("test-city");
        address.setRegion("test-region");
        address.setDetailAddress("test-address");
        when(addressService.getItem(1L)).thenReturn(address);
        when(redisService.incr(any(String.class), anyLong())).thenReturn(1L);
        when(orderSettingMapper.selectByExample(any())).thenReturn(List.of());
        OmsOrderSetting setting = new OmsOrderSetting();
        setting.setNormalOrderOvertime(30);
        when(orderSettingMapper.selectByPrimaryKey(1L)).thenReturn(setting);

        OrderParam request = new OrderParam();
        request.setMemberReceiveAddressId(1L);
        request.setPayType(1);
        request.setCartIds(List.of(1L));

        try {
            JsonNode firstResult = submitOrder(idempotencyKey, request);
            createdOrder = objectMapper.treeToValue(firstResult.path("data").path("order"), OmsOrder.class);
            assertTrue(createdOrder.getId() != null);
            assertEquals(2, readLockStock(skuId));
            assertEquals(1, jdbcTemplate.queryForObject(
                    "SELECT COUNT(*) FROM oms_order WHERE id = ?", Integer.class, createdOrder.getId()));
            assertEquals(1, jdbcTemplate.queryForObject(
                    "SELECT COUNT(*) FROM oms_order_item WHERE order_id = ?", Integer.class, createdOrder.getId()));

            JsonNode retryResult = submitOrder(idempotencyKey, request);
            OmsOrder replayedOrder = objectMapper.treeToValue(retryResult.path("data").path("order"), OmsOrder.class);
            assertEquals(createdOrder.getId(), replayedOrder.getId());
            assertEquals(createdOrder.getOrderSn(), replayedOrder.getOrderSn());
            assertEquals(1, retryResult.path("data").path("orderItemList").size());
            assertEquals(2, readLockStock(skuId));
            assertEquals(1, jdbcTemplate.queryForObject(
                    "SELECT COUNT(*) FROM oms_order WHERE member_id = ?", Integer.class, memberId));

            OrderParam changedRequest = new OrderParam();
            changedRequest.setMemberReceiveAddressId(1L);
            changedRequest.setPayType(2);
            changedRequest.setCartIds(List.of(1L));
            JsonNode conflictResult = submitOrder(idempotencyKey, changedRequest);
            assertTrue(conflictResult.path("code").asLong() != 200L);
            assertEquals(2, readLockStock(skuId));
            assertEquals(1, jdbcTemplate.queryForObject(
                    "SELECT COUNT(*) FROM oms_order WHERE member_id = ?", Integer.class, memberId));
            verify(cartItemService, times(1)).listPromotion(memberId, List.of(1L));
            assertEquals(1, jdbcTemplate.queryForObject(
                    "SELECT COUNT(*) FROM oms_outbox_event WHERE order_id = ? AND event_type = 'ORDER_CANCEL_DELAY'",
                    Integer.class, createdOrder.getId()));
        } finally {
            if (createdOrder != null && createdOrder.getId() != null) {
                jdbcTemplate.update("DELETE FROM oms_outbox_event WHERE order_id = ?", createdOrder.getId());
                jdbcTemplate.update("DELETE FROM oms_order_item WHERE order_id = ?", createdOrder.getId());
                jdbcTemplate.update("DELETE FROM oms_order WHERE id = ?", createdOrder.getId());
            }
            jdbcTemplate.update("DELETE FROM oms_order_idempotency WHERE member_id = ? AND idempotency_key = ?",
                    memberId, idempotencyKey);
            deleteSku(skuId);
        }
    }

    @Test
    void outboxEventMustRollbackWithItsDatabaseTransaction() {
        long orderId = ThreadLocalRandom.current().nextLong(1, Long.MAX_VALUE);
        TransactionTemplate transaction = new TransactionTemplate(transactionManager);

        assertThrows(IllegalStateException.class, () -> transaction.execute(status -> {
            outboxEventDao.insertCancelOrderEvent(orderId, 5000L);
            throw new IllegalStateException("force transaction rollback");
        }));
        assertEquals(0, jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM oms_outbox_event WHERE order_id = ?", Integer.class, orderId));
    }

    @Test
    void outboxPublisherMustConfirmAndRetryEvents() throws Exception {
        long publishedOrderId = ThreadLocalRandom.current().nextLong(1, Long.MAX_VALUE);
        long failedOrderId = ThreadLocalRandom.current().nextLong(1, Long.MAX_VALUE);
        outboxEventDao.insertCancelOrderEvent(publishedOrderId, 5000L);
        outboxEventDao.insertCancelOrderEvent(failedOrderId, 7000L);
        Long publishedEventId = jdbcTemplate.queryForObject(
                "SELECT id FROM oms_outbox_event WHERE order_id = ? ORDER BY id DESC LIMIT 1",
                Long.class, publishedOrderId);
        Long failedEventId = jdbcTemplate.queryForObject(
                "SELECT id FROM oms_outbox_event WHERE order_id = ? ORDER BY id DESC LIMIT 1",
                Long.class, failedOrderId);
        doThrow(new IllegalStateException("simulated broker outage"))
                .when(cancelOrderSender).sendMessage(failedOrderId, 7000L, failedEventId.toString());

        try {
            new OutboxEventPublisher(outboxEventDao, cancelOrderSender).publishDueEvents();

            assertEquals("PUBLISHED", jdbcTemplate.queryForObject(
                    "SELECT status FROM oms_outbox_event WHERE id = ?", String.class, publishedEventId));
            assertEquals("RETRY", jdbcTemplate.queryForObject(
                    "SELECT status FROM oms_outbox_event WHERE id = ?", String.class, failedEventId));
            assertEquals(1, jdbcTemplate.queryForObject(
                    "SELECT attempts FROM oms_outbox_event WHERE id = ?", Integer.class, failedEventId));
            verify(cancelOrderSender, times(1)).sendMessage(publishedOrderId, 5000L, publishedEventId.toString());
        } finally {
            jdbcTemplate.update("DELETE FROM oms_outbox_event WHERE id IN (?, ?)", publishedEventId, failedEventId);
        }
    }

    @Test
    void generateOrderEndpointMustRequireIdempotencyHeader() throws Exception {
        OrderParam request = new OrderParam();
        request.setMemberReceiveAddressId(1L);
        request.setPayType(1);
        request.setCartIds(List.of(1L));

        mockMvc.perform(post("/order/generateOrder")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(objectMapper.writeValueAsBytes(request)))
                .andExpect(status().isBadRequest());
    }

    private JsonNode submitOrder(String idempotencyKey, OrderParam request) throws Exception {
        MvcResult result = mockMvc.perform(post("/order/generateOrder")
                        .header("Idempotency-Key", idempotencyKey)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(objectMapper.writeValueAsBytes(request)))
                .andExpect(status().isOk())
                .andReturn();
        return objectMapper.readTree(result.getResponse().getContentAsByteArray());
    }

    private long createSku(int stock) {
        return createSku(stock, 0);
    }

    private long createSku(int stock, int lockStock) {
        String skuCode = "test-" + UUID.randomUUID();
        KeyHolder keyHolder = new GeneratedKeyHolder();
        PreparedStatementCreator creator = (Connection connection) -> {
            PreparedStatement statement = connection.prepareStatement(
                    "INSERT INTO pms_sku_stock (product_id, sku_code, stock, lock_stock) VALUES (NULL, ?, ?, ?)",
                    Statement.RETURN_GENERATED_KEYS);
            statement.setString(1, skuCode);
            statement.setInt(2, stock);
            statement.setInt(3, lockStock);
            return statement;
        };
        jdbcTemplate.update(creator, keyHolder);
        Number key = keyHolder.getKey();
        if (key == null) {
            throw new IllegalStateException("Test SKU insert returned no primary key");
        }
        return key.longValue();
    }

    private long createPendingOrder(Long memberId, String orderSn, Long skuId) {
        KeyHolder keyHolder = new GeneratedKeyHolder();
        PreparedStatementCreator creator = connection -> {
            PreparedStatement statement = connection.prepareStatement(
                    "INSERT INTO oms_order (member_id, order_sn, create_time, status, delete_status, receiver_name, receiver_phone) " +
                            "VALUES (?, ?, NOW(), 0, 0, 'test-recipient', '10000000000')",
                    Statement.RETURN_GENERATED_KEYS);
            statement.setLong(1, memberId);
            statement.setString(2, orderSn);
            return statement;
        };
        jdbcTemplate.update(creator, keyHolder);
        Number key = keyHolder.getKey();
        if (key == null) {
            throw new IllegalStateException("Test order insert returned no primary key");
        }
        long orderId = key.longValue();
        jdbcTemplate.update("INSERT INTO oms_order_item (order_id, order_sn, product_name, product_sku_id, product_sku_code, product_price, product_quantity) " +
                        "VALUES (?, ?, 'transition-test-product', ?, ?, 10.00, 1)",
                orderId, orderSn, skuId, "test-sku-" + skuId);
        return orderId;
    }

    private int readLockStock(long skuId) {
        Integer lockStock = jdbcTemplate.queryForObject(
                "SELECT lock_stock FROM pms_sku_stock WHERE id = ?", Integer.class, skuId);
        return lockStock == null ? 0 : lockStock;
    }

    private int readAvailableStock(long skuId) {
        Integer availableStock = jdbcTemplate.queryForObject(
                "SELECT stock - lock_stock FROM pms_sku_stock WHERE id = ?", Integer.class, skuId);
        return availableStock == null ? 0 : availableStock;
    }

    private void deleteSku(long skuId) {
        jdbcTemplate.update("DELETE FROM pms_sku_stock WHERE id = ?", skuId);
    }
}
