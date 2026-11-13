package com.macro.mall.portal.domain;

import lombok.Data;

import java.util.Date;

/**
 * 下单请求的幂等记录。
 */
@Data
public class OmsOrderIdempotency {
    private Long id;
    private Long memberId;
    private String idempotencyKey;
    private String requestHash;
    private Long orderId;
    private String status;
    private Date createTime;
    private Date updateTime;
}
