package com.macro.mall.portal.domain;

import lombok.Data;

import java.util.Date;

/**
 * 待投递到消息队列的本地事务事件。
 */
@Data
public class OmsOutboxEvent {
    private Long id;
    private Long orderId;
    private Long delayMillis;
    private String status;
    private Integer attempts;
    private Date nextAttemptTime;
    private Date lockedUntil;
    private String lastError;
    private Date createTime;
    private Date updateTime;
}
