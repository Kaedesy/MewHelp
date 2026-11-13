package com.macro.mall.portal.component;

import com.macro.mall.portal.domain.QueueEnum;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.amqp.AmqpException;
import org.springframework.amqp.rabbit.connection.CorrelationData;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.core.MessagePostProcessor;
import org.springframework.amqp.core.ReturnedMessage;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;

import java.util.concurrent.TimeUnit;

/**
 * 取消订单消息的发出者
 * Created by macro on 2018/9/14.
 */
@Component
public class CancelOrderSender {
    private static final Logger LOGGER = LoggerFactory.getLogger(CancelOrderSender.class);
    @Autowired
    private RabbitTemplate rabbitTemplate;

    /** 将取消事件发给延迟队列，并等待 RabbitMQ 的 Publisher Confirm。 */
    public void sendMessage(Long orderId, long delayTimes, String eventId) throws Exception {
        CorrelationData correlationData = new CorrelationData(eventId);
        rabbitTemplate.convertAndSend(QueueEnum.QUEUE_TTL_ORDER_CANCEL.getExchange(),
                QueueEnum.QUEUE_TTL_ORDER_CANCEL.getRouteKey(), orderId, new MessagePostProcessor() {
            @Override
            public Message postProcessMessage(Message message) throws AmqpException {
                message.getMessageProperties().setExpiration(String.valueOf(delayTimes));
                message.getMessageProperties().setMessageId(eventId);
                return message;
            }
        }, correlationData);

        CorrelationData.Confirm confirm = correlationData.getFuture().get(10, TimeUnit.SECONDS);
        if (!confirm.isAck()) {
            throw new IllegalStateException("RabbitMQ rejected outbox event " + eventId + ": " + confirm.getReason());
        }
        ReturnedMessage returned = correlationData.getReturned();
        if (returned != null) {
            throw new IllegalStateException("RabbitMQ could not route outbox event " + eventId
                    + ": " + returned.getReplyText());
        }
        LOGGER.info("Published cancel event {}, orderId:{}", eventId, orderId);
    }
}
