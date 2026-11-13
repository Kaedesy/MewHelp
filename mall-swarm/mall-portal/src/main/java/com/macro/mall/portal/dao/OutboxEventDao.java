package com.macro.mall.portal.dao;

import com.macro.mall.portal.domain.OmsOutboxEvent;
import org.apache.ibatis.annotations.Param;

import java.util.List;

public interface OutboxEventDao {
    int insertCancelOrderEvent(@Param("orderId") Long orderId,
                               @Param("delayMillis") Long delayMillis);

    List<Long> selectDueEventIds(@Param("limit") Integer limit);

    int claimEvent(@Param("eventId") Long eventId,
                   @Param("leaseSeconds") Integer leaseSeconds);

    OmsOutboxEvent selectById(@Param("eventId") Long eventId);

    int markPublished(@Param("eventId") Long eventId);

    int markFailed(@Param("eventId") Long eventId,
                   @Param("maxAttempts") Integer maxAttempts,
                   @Param("delaySeconds") Integer delaySeconds,
                   @Param("error") String error);
}
