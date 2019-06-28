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
	})
	
	$('.pricing-table-tabs-menu ul li').click(function(){
		var tab_id = $(this).attr('data-tab');

		$('.pricing-table-tabs-menu ul li').removeClass('current');
		$('.tab-content').removeClass('current');

		$(this).addClass('current');
		$("#"+tab_id).addClass('current');
	})
	
});
