package com.macro.mall.portal.dao;

import com.macro.mall.model.OmsOrderItem;
import com.macro.mall.portal.domain.OmsOrderDetail;
import com.macro.mall.portal.domain.OmsOrderIdempotency;
import org.apache.ibatis.annotations.Param;

import java.util.List;

/**
 * 前台订单自定义Dao
 * Created by macro on 2018/9/4.
 */
public interface PortalOrderDao {
    /**
     * 获取订单及下单商品详情
     */
    OmsOrderDetail getDetail(@Param("orderId") Long orderId);

    /**
     * 修改 pms_sku_stock表的锁定库存及真实库存
     */
    int updateSkuStock(@Param("itemList") List<OmsOrderItem> orderItemList);

    /**
     * 在数据库中原子校验可售库存并预占指定 SKU。
     */
    int lockSkuStock(@Param("skuId") Long skuId, @Param("quantity") Integer quantity);

    /**
     * 首次认领某会员的下单幂等键；已存在时不创建第二条记录。
     */
    int insertIdempotencyRequest(@Param("memberId") Long memberId,
                                 @Param("idempotencyKey") String idempotencyKey,
                                 @Param("requestHash") String requestHash);

    /**
     * 读取并锁定下单幂等记录，保证并发重试读取到已提交的当前状态。
     */
    OmsOrderIdempotency selectIdempotencyRequestForUpdate(@Param("memberId") Long memberId,
                                                          @Param("idempotencyKey") String idempotencyKey);

    /**
     * 将处理中的幂等记录绑定到成功创建的订单。
     */
    int completeIdempotencyRequest(@Param("memberId") Long memberId,
                                   @Param("idempotencyKey") String idempotencyKey,
                                   @Param("requestHash") String requestHash,
                                   @Param("orderId") Long orderId);

    /**
     * 获取超时订单
     * @param minute 超时时间（分）
     */
    List<OmsOrderDetail> getTimeOutOrders(@Param("minute") Integer minute);

    /**
     * 批量修改订单状态
     */
    int updateOrderStatus(@Param("ids") List<Long> ids,@Param("status") Integer status);

    /**
     * 解除取消订单的库存锁定
     */
    int releaseSkuStockLock(@Param("itemList") List<OmsOrderItem> orderItemList);

}
