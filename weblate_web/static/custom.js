$(function () {
	$('.menu-show').click(function(e) {
		var $body = $('body');
		$body.toggleClass('open-mobile');
	});
	$('.open-langs').click(function(e) {
		if($(this).parent().hasClass('opened')){
			$(this).next().fadeOut(300);
			$(this).parent().removeClass('opened');
		} else {
			$(this).next().fadeIn(300);
			$(this).parent().addClass('opened');
		}
		return false;
	});

	$('ul.pricing-tabs li').click(function(){
		var tab_id = $(this).attr('data-tab');

		$('ul.pricing-tabs li').removeClass('current');
		$('.tab-content').removeClass('current');

		$(this).addClass('current');
		$("#"+tab_id).addClass('current');
	});

	$('.pricing-table-tabs-menu ul li').click(function(){
		var tab_id = $(this).attr('data-tab');

		$('.pricing-table-tabs-menu ul li').removeClass('current');
		$('.tab-content').removeClass('current');

		$(this).addClass('current');
		$("#"+tab_id).addClass('current');
	});

    $('.rewards .choose').click(function() {
        var container = $($(this).parents('.reward')[0]);
        container.parent().find('.reward').removeClass('checked');
        container.addClass('checked');
        container.find('input').prop('checked', true);
        return false;
    });
    $('.rewards .close').click(function() {
        $('.rewards .fourth .choose').click();
        return false;
    });
    $('#donate-amount').change(function() {
        var amount = $(this).val();
        var found = 0;
        $('.reward').each(function () {
            var $this = $(this);
            if ($this.data('amount') <= amount) {
                if ($this.hasClass('small')) {
                    $this.removeClass('small');
                    $this.find('.choose').click();
                }
                found++;
            } else {
                $this.addClass('small');
                if ($this.hasClass('checked')) {
                    $this.find('.close').click();
                }
            }
        });
        console.log(found);
        if (found > 1) {
            $('.whoa').show();
            $('.nowhoa').hide();
        } else {
            $('.whoa').hide();
            $('.nowhoa').show();
        }
    });
    $('#donate-amount').change();
});
