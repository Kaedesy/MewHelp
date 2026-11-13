package com.macro.mall.portal.component;

import com.macro.mall.portal.dao.OutboxEventDao;
import com.macro.mall.portal.domain.OmsOutboxEvent;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.util.List;

/** 扫描本地 Outbox 并至少一次投递到 RabbitMQ。 */
@Component
@ConditionalOnProperty(name = "mall.outbox.enabled", havingValue = "true", matchIfMissing = true)
public class OutboxEventPublisher {
    private static final Logger LOGGER = LoggerFactory.getLogger(OutboxEventPublisher.class);
    private static final int BATCH_SIZE = 20;
    private static final int MAX_ATTEMPTS = 12;
    private static final int LEASE_SECONDS = 60;

    private final OutboxEventDao outboxEventDao;
    private final CancelOrderSender cancelOrderSender;

    public OutboxEventPublisher(OutboxEventDao outboxEventDao, CancelOrderSender cancelOrderSender) {
        this.outboxEventDao = outboxEventDao;
        this.cancelOrderSender = cancelOrderSender;
    }

    @Scheduled(fixedDelayString = "${mall.outbox.poll-interval-ms:1000}")
    public void publishDueEvents() {
        List<Long> eventIds = outboxEventDao.selectDueEventIds(BATCH_SIZE);
        for (Long eventId : eventIds) {
            if (outboxEventDao.claimEvent(eventId, LEASE_SECONDS) != 1) {
                continue;
            }
            publishClaimedEvent(eventId);
        }
    }

    private void publishClaimedEvent(Long eventId) {
        OmsOutboxEvent event = outboxEventDao.selectById(eventId);
        if (event == null) {
            LOGGER.error("Claimed outbox event disappeared: {}", eventId);
            return;
        }
        try {
            cancelOrderSender.sendMessage(event.getOrderId(), event.getDelayMillis(), eventId.toString());
            if (outboxEventDao.markPublished(eventId) != 1) {
                LOGGER.error("RabbitMQ confirmed event {}, but its Outbox state was not updated", eventId);
            }
        } catch (Exception exception) {
            int attempts = event.getAttempts() == null ? 1 : event.getAttempts();
            int delaySeconds = (int) Math.min(300L, 1L << Math.min(Math.max(attempts - 1, 0), 8));
            try {
                int updated = outboxEventDao.markFailed(eventId, MAX_ATTEMPTS, delaySeconds,
                        summarize(exception));
                if (updated == 1 && attempts >= MAX_ATTEMPTS) {
                    LOGGER.error("Outbox event {} reached retry limit and needs manual replay", eventId, exception);
                } else {
                    LOGGER.warn("Outbox event {} failed; retrying in {} seconds", eventId, delaySeconds, exception);
                }
            } catch (Exception updateFailure) {
                LOGGER.error("Could not persist retry state for outbox event {}", eventId, updateFailure);
            }
        }
    }

    private String summarize(Exception exception) {
        String message = exception.getClass().getSimpleName() + ": " + exception.getMessage();
        return message.length() > 1000 ? message.substring(0, 1000) : message;
    }
}
