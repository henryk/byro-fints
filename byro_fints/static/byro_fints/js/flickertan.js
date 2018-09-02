$(function(){
    var Flicker = function(fdiv) {
        var interval = 25;
        var data = $(fdiv).data('flicker-content');
        var main = $(fdiv).find('.flicker-main');

        var outstream = ['1', '0', '31', '30', '31', '30'];
        for(var i=0; i<data.length; i++) {
            d = parseInt( data.charAt( i ^ 1 ), 16);
            outstream.push(1 | (d << 1) );
            outstream.push(d << 1);
        }
        var index = 0;

        var update = function() {
            var output = outstream[index];
            index++;
            if(index >= outstream.length) {
                index = 0;
            }
            main.attr('class', 'flicker-main flicker-data-'+output);
            window.setTimeout(update, interval);
        };

        var start = function() {
            $(fdiv).addClass('flicker-animate-js');
            update();
        };
        //start();
    };
    $('.flicker-code').each(function(){Flicker(this)});
});
